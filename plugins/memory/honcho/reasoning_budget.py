"""Provider-local guardrails for paid Honcho reasoning calls.

This module deliberately has no Honcho SDK or network dependency.  The provider
passes one already-bound SDK callable to :class:`PaidReasoningGuard`; the guard
makes the policy decision, atomically consumes an opaque tool-call reservation,
and invokes that callable at most once.

The reservation and receipt interfaces are intentionally tiny so production code
can supply durable implementations while tests can use the in-memory fakes in
this module.  A reservation is never released: a tool-call ID is a one-shot
spend authority, including after a failure or timeout.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol


# Reasoning tiers are strings because they are passed through to the Honcho SDK.
MINIMAL = "minimal"
LOW = "low"
MEDIUM = "medium"
HIGH = "high"
MAX = "max"
VALID_TIERS = (MINIMAL, LOW, MEDIUM, HIGH, MAX)
INTERACTIVE_TIERS = frozenset((MINIMAL, LOW, MEDIUM))

INTERACTIVE = "interactive"
OFFLINE = "offline"
VALID_MODES = frozenset((INTERACTIVE, OFFLINE))

DENIED = "denied"
SUCCESS = "success"
FAILURE = "failure"
TIMEOUT = "timeout"


class _UnknownOutcomeMarker(str):
    """A string status that is also safe to return as an SDK result marker."""


UNKNOWN_OUTCOME = _UnknownOutcomeMarker("unknown_outcome")


class UnknownOutcomeError(RuntimeError):
    """Signal that the provider cannot tell whether the paid call took effect."""


# Compatibility aliases make the exception name explicit at call sites without
# creating a second outcome type.
UnknownOutcomeException = UnknownOutcomeError


class ReservationStore(Protocol):
    """Atomic, non-releasing one-shot reservation interface."""

    def reserve(self, call_id: str, tier: str) -> bool:
        """Return True exactly once for a previously unseen trusted call ID."""


class ReceiptSink(Protocol):
    """Sink for terminal paid-call receipts."""

    def record(self, receipt: "ReasoningReceipt") -> bool | None:
        """Persist a receipt; return False when it could not be persisted."""


@dataclass(frozen=True)
class ReasoningReceipt:
    """Terminal, content-free record for one reserved paid call."""

    call_id: str
    tier: str
    status: str
    recorded_at: float
    reason: str | None = None
    error_type: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {SUCCESS, FAILURE, TIMEOUT, UNKNOWN_OUTCOME}:
            raise ValueError(f"invalid terminal receipt status: {self.status!r}")


@dataclass(frozen=True)
class PolicyDecision:
    """Result of validating a paid-reasoning request before reservation."""

    allowed: bool
    tier: str
    reason: str = ""


@dataclass(frozen=True)
class GuardResult:
    """Result returned by :meth:`PaidReasoningGuard.execute`.

    ``status`` is ``denied`` when no reservation was obtained.  For a reserved
    call it is one of the four terminal receipt statuses.  No late worker value
    is ever copied into this object after it has been returned.
    """

    allowed: bool
    status: str
    call_id: str
    tier: str
    value: Any = field(default=None, repr=False)
    error: BaseException | None = field(default=None, repr=False)
    reason: str = ""
    receipt: ReasoningReceipt | None = field(default=None, repr=False)
    worker_thread: threading.Thread | None = field(default=None, repr=False)

    @property
    def outcome(self) -> str:
        """Alias useful to callers that call terminal statuses outcomes."""
        return self.status

    @property
    def denied(self) -> bool:
        return not self.allowed


@dataclass(frozen=True)
class OfflineManifestEntry:
    """One exact, pre-enumerated offline paid-call authority."""

    call_id: str
    tier: str


class OfflineReasoningManifest:
    """Immutable exact-match manifest for offline high/max reasoning.

    Entries are keyed by the opaque trusted tool-call ID and include the exact
    tier.  A high-tier request cannot borrow a manifest entry for another ID or
    another tier; max additionally needs its matching approval token.
    """

    def __init__(
        self,
        entries: Mapping[str, str]
        | Iterable[OfflineManifestEntry | Mapping[str, str] | tuple[str, str]]
        | None = None,
    ) -> None:
        parsed: dict[str, str] = {}
        if entries is None:
            source: Iterable[Any] = ()
        elif isinstance(entries, Mapping):
            source = entries.items()
        else:
            source = entries

        for raw_entry in source:
            if isinstance(raw_entry, OfflineManifestEntry):
                call_id, tier = raw_entry.call_id, raw_entry.tier
            elif isinstance(raw_entry, Mapping):
                call_id = raw_entry.get("call_id") or raw_entry.get("tool_call_id")
                tier = raw_entry.get("tier")
            else:
                try:
                    call_id, tier = raw_entry
                except (TypeError, ValueError) as exc:
                    raise ValueError("manifest entries must contain call_id and tier") from exc

            if not isinstance(call_id, str) or not call_id:
                raise ValueError("manifest call_id must be a non-empty string")
            if tier not in VALID_TIERS:
                raise ValueError(f"invalid manifest tier: {tier!r}")
            previous = parsed.get(call_id)
            if previous is not None and previous != tier:
                raise ValueError("a call_id cannot be enumerated for two tiers")
            parsed[call_id] = tier
        self._entries = parsed

    def permits(self, call_id: str, tier: str) -> bool:
        return self._entries.get(call_id) == tier

    def contains(self, call_id: str, tier: str) -> bool:
        return self.permits(call_id, tier)

    def entries(self) -> tuple[OfflineManifestEntry, ...]:
        return tuple(OfflineManifestEntry(call_id, tier) for call_id, tier in self._entries.items())

    def __contains__(self, item: object) -> bool:
        if isinstance(item, tuple) and len(item) == 2:
            return self.permits(item[0], item[1])
        return False


def _secret_bytes(secret: bytes | bytearray | str) -> bytes:
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    elif isinstance(secret, bytearray):
        secret = bytes(secret)
    if not isinstance(secret, bytes) or not secret:
        raise ValueError("hmac_secret must be non-empty bytes or text")
    return secret


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


_CALL_ID_PREFIX = "hrb1"
_APPROVAL_PREFIX = "hra1"


def issue_trusted_tool_call_id(secret: bytes | bytearray | str) -> str:
    """Issue a random opaque HMAC-backed tool-call ID.

    The output contains only random nonce bytes and their HMAC.  It never
    embeds a query, prompt, answer, or other paid-call content.
    """

    key = _secret_bytes(secret)
    nonce = secrets.token_bytes(24)
    nonce_part = _b64(nonce)
    body = f"{_CALL_ID_PREFIX}.{nonce_part}".encode("ascii")
    signature = _b64(hmac.new(key, body, hashlib.sha256).digest())
    return f"{body.decode('ascii')}.{signature}"


def is_trusted_tool_call_id(secret: bytes | bytearray | str, call_id: str) -> bool:
    """Verify an opaque HMAC-backed tool-call ID in constant time."""

    try:
        key = _secret_bytes(secret)
        if not isinstance(call_id, str):
            return False
        prefix, nonce_part, signature = call_id.split(".", 2)
        if prefix != _CALL_ID_PREFIX or not nonce_part or not signature:
            return False
        # Decode both pieces so malformed/non-canonical tokens are rejected.
        nonce = _unb64(nonce_part)
        supplied = _unb64(signature)
        if len(nonce) != 24 or len(supplied) != hashlib.sha256().digest_size:
            return False
        body = f"{prefix}.{nonce_part}".encode("ascii")
        expected = hmac.new(key, body, hashlib.sha256).digest()
        return hmac.compare_digest(supplied, expected)
    except (TypeError, ValueError, UnicodeError):
        return False


def issue_explicit_approval(
    secret: bytes | bytearray | str,
    *,
    call_id: str,
    tier: str,
) -> str:
    """Issue the exact approval token for one opaque call ID and tier.

    The token is itself content-free.  Verification binds it to both the
    enumerated ID and the requested tier, so a max approval cannot be replayed
    for another call or used to escalate a high call to max.
    """

    key = _secret_bytes(secret)
    payload = f"{_APPROVAL_PREFIX}\0{call_id}\0{tier}".encode("utf-8")
    return f"{_APPROVAL_PREFIX}.{_b64(hmac.new(key, payload, hashlib.sha256).digest())}"


def _is_exact_approval(
    secret: bytes | bytearray | str,
    approval: str | None,
    *,
    call_id: str,
    tier: str,
) -> bool:
    if not isinstance(approval, str):
        return False
    try:
        expected = issue_explicit_approval(secret, call_id=call_id, tier=tier)
        return hmac.compare_digest(approval.encode("ascii"), expected.encode("ascii"))
    except (UnicodeEncodeError, TypeError, ValueError):
        return False


def _coerce_manifest(manifest: OfflineReasoningManifest | Mapping[str, str] | Iterable[Any] | None) -> OfflineReasoningManifest:
    if isinstance(manifest, OfflineReasoningManifest):
        return manifest
    return OfflineReasoningManifest(manifest)


def validate_reasoning_request(
    tier: str,
    *,
    mode: str,
    tool_call_id: str,
    hmac_secret: bytes | bytearray | str,
    offline_manifest: OfflineReasoningManifest | Mapping[str, str] | Iterable[Any] | None = None,
    explicit_approval: str | None = None,
) -> PolicyDecision:
    """Validate tier, execution mode, trusted ID, manifest, and approval."""

    if tier not in VALID_TIERS:
        return PolicyDecision(False, tier, f"invalid reasoning tier: {tier!r}")
    if mode not in VALID_MODES:
        return PolicyDecision(False, tier, f"invalid reasoning mode: {mode!r}")
    if not is_trusted_tool_call_id(hmac_secret, tool_call_id):
        return PolicyDecision(False, tier, "untrusted tool-call ID")

    if mode == INTERACTIVE:
        if tier not in INTERACTIVE_TIERS:
            return PolicyDecision(False, tier, f"tier {tier!r} is not allowed interactively")
        return PolicyDecision(True, tier)

    manifest = _coerce_manifest(offline_manifest)
    if tier in {HIGH, MAX} and not manifest.permits(tool_call_id, tier):
        return PolicyDecision(False, tier, "offline manifest does not enumerate this call ID/tier")
    if tier == MAX and not _is_exact_approval(
        hmac_secret,
        explicit_approval,
        call_id=tool_call_id,
        tier=tier,
    ):
        return PolicyDecision(False, tier, "max reasoning requires exact explicit approval")
    return PolicyDecision(True, tier)


# A short alias is useful for callers that phrase the policy operation as tier
# validation; the full name remains the canonical API.
validate_tier = validate_reasoning_request


@dataclass
class _Invocation:
    lock: threading.Lock
    done: threading.Event
    settled: bool = False
    finished: bool = False
    finished_at: float | None = None
    status: str | None = None
    value: Any = None
    error: BaseException | None = None
    reason: str = ""
    receipt: ReasoningReceipt | None = None


class InMemoryReservationStore:
    """Thread-safe one-shot reservation fake for tests and local embedding."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reserved: dict[str, str] = {}

    def reserve(self, call_id: str, tier: str) -> bool:
        with self._lock:
            if call_id in self._reserved:
                return False
            self._reserved[call_id] = tier
            return True

    def contains(self, call_id: str) -> bool:
        with self._lock:
            return call_id in self._reserved

    def reservations(self) -> list[OfflineManifestEntry]:
        with self._lock:
            return [OfflineManifestEntry(call_id, tier) for call_id, tier in self._reserved.items()]


class InMemoryReceiptSink:
    """Thread-safe receipt fake that keeps the first terminal receipt per ID."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._receipts: dict[str, ReasoningReceipt] = {}

    def record(self, receipt: ReasoningReceipt) -> bool:
        with self._lock:
            if receipt.call_id in self._receipts:
                return False
            self._receipts[receipt.call_id] = receipt
            return True

    # Sink-shaped aliases make the fake convenient for dependency injection.
    emit = record
    write = record

    def get(self, call_id: str) -> ReasoningReceipt | None:
        with self._lock:
            return self._receipts.get(call_id)

    def receipts(self) -> list[ReasoningReceipt]:
        with self._lock:
            return list(self._receipts.values())


class PaidReasoningGuard:
    """Reserve and execute exactly one provider-local paid reasoning call."""

    def __init__(
        self,
        hmac_secret: bytes | bytearray | str | None = None,
        *,
        secret: bytes | bytearray | str | None = None,
        receipt_sink: ReceiptSink | Callable[[ReasoningReceipt], Any] | None = None,
        receipts: ReceiptSink | Callable[[ReasoningReceipt], Any] | None = None,
        reservation_store: ReservationStore | None = None,
        offline_manifest: OfflineReasoningManifest | Mapping[str, str] | Iterable[Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if hmac_secret is not None and secret is not None:
            raise TypeError("pass hmac_secret or secret, not both")
        self._secret = _secret_bytes(hmac_secret if hmac_secret is not None else secret)  # type: ignore[arg-type]
        selected_sink = receipt_sink if receipt_sink is not None else receipts
        self.receipt_sink = selected_sink or InMemoryReceiptSink()
        self.reservation_store = reservation_store or InMemoryReservationStore()
        self.offline_manifest = _coerce_manifest(offline_manifest)
        self._clock = clock

    def _record_terminal(
        self,
        call_id: str,
        tier: str,
        status: str,
        *,
        reason: str = "",
        error: BaseException | None = None,
    ) -> tuple[str, ReasoningReceipt]:
        receipt = ReasoningReceipt(
            call_id=call_id,
            tier=tier,
            status=status,
            recorded_at=self._clock(),
            reason=reason or None,
            error_type=type(error).__name__ if error is not None else None,
        )
        writer = getattr(self.receipt_sink, "record", None)
        if writer is None:
            writer = getattr(self.receipt_sink, "emit", None)
        if writer is None:
            writer = getattr(self.receipt_sink, "write", None)
        if writer is None and callable(self.receipt_sink):
            writer = self.receipt_sink
        try:
            persisted = writer(receipt) if writer is not None else False
        except Exception:
            persisted = False
        if persisted is False:
            # The SDK outcome is no longer safely represented by the receipt
            # sink.  Do not retry the sink: surface one local unknown outcome.
            receipt = ReasoningReceipt(
                call_id=call_id,
                tier=tier,
                status=UNKNOWN_OUTCOME,
                recorded_at=self._clock(),
                reason="terminal receipt could not be persisted",
            )
            return UNKNOWN_OUTCOME, receipt
        return status, receipt

    def _denied(self, call_id: str, tier: str, reason: str) -> GuardResult:
        return GuardResult(
            allowed=False,
            status=DENIED,
            call_id=call_id,
            tier=tier,
            reason=reason,
        )

    @staticmethod
    def _classify(value: Any = None, error: BaseException | None = None) -> tuple[str, str]:
        if error is not None:
            if isinstance(error, UnknownOutcomeError) or getattr(error, "unknown_outcome", False):
                return UNKNOWN_OUTCOME, "provider outcome is unknown"
            if isinstance(error, TimeoutError):
                return TIMEOUT, "provider call timed out"
            return FAILURE, "provider call failed"
        if value is UNKNOWN_OUTCOME:
            return UNKNOWN_OUTCOME, "provider outcome is unknown"
        return SUCCESS, "provider call completed"

    def _settle(
        self,
        invocation: _Invocation,
        call_id: str,
        tier: str,
        status: str,
        *,
        value: Any = None,
        error: BaseException | None = None,
        reason: str,
    ) -> None:
        final_status, receipt = self._record_terminal(
            call_id,
            tier,
            status,
            reason=reason,
            error=error,
        )
        invocation.status = final_status
        invocation.receipt = receipt
        invocation.reason = reason if final_status == status else "terminal receipt could not be persisted"
        invocation.value = value if final_status == status else None
        invocation.error = error
        invocation.settled = True

    def execute(
        self,
        operation: Callable[[], Any] | None = None,
        *,
        call: Callable[[], Any] | None = None,
        sdk_call: Callable[[], Any] | None = None,
        tier: str,
        tool_call_id: str | None = None,
        call_id: str | None = None,
        mode: str = INTERACTIVE,
        offline_manifest: OfflineReasoningManifest | Mapping[str, str] | Iterable[Any] | None = None,
        explicit_approval: str | None = None,
        deadline: float | None = None,
        timeout: float | None = None,
    ) -> GuardResult:
        """Validate, reserve, and invoke the supplied callable at most once.

        ``deadline`` is an absolute value from the guard's monotonic clock.
        ``timeout`` is a convenience relative duration and cannot be combined
        with ``deadline``.  On timeout the daemon worker is intentionally
        not joined; its eventual result is discarded by the invocation-local
        settlement gate.
        """

        supplied = [candidate for candidate in (operation, call, sdk_call) if candidate is not None]
        if len(supplied) != 1:
            resolved_call_id = tool_call_id if tool_call_id is not None else (call_id or "")
            return self._denied(resolved_call_id, tier, "exactly one callable must be supplied")
        operation = supplied[0]

        resolved_call_id = tool_call_id if tool_call_id is not None else call_id
        if not isinstance(resolved_call_id, str):
            return self._denied("", tier, "trusted tool-call ID is required")
        if deadline is not None and timeout is not None:
            return self._denied(resolved_call_id, tier, "deadline and timeout cannot both be supplied")
        if timeout is not None:
            try:
                deadline = self._clock() + max(0.0, float(timeout))
            except (TypeError, ValueError):
                return self._denied(resolved_call_id, tier, "invalid timeout")

        manifest = self.offline_manifest if offline_manifest is None else _coerce_manifest(offline_manifest)
        policy = validate_reasoning_request(
            tier,
            mode=mode,
            tool_call_id=resolved_call_id,
            hmac_secret=self._secret,
            offline_manifest=manifest,
            explicit_approval=explicit_approval,
        )
        if not policy.allowed:
            return self._denied(resolved_call_id, tier, policy.reason)

        try:
            reserved = self.reservation_store.reserve(resolved_call_id, tier)
        except Exception:
            reserved = False
        if not reserved:
            duplicate = False
            contains = getattr(self.reservation_store, "contains", None)
            if contains is not None:
                try:
                    duplicate = bool(contains(resolved_call_id))
                except Exception:
                    duplicate = False
            reason = "duplicate trusted tool-call ID" if duplicate else "reservation failed"
            return self._denied(resolved_call_id, tier, reason)

        invocation = _Invocation(lock=threading.Lock(), done=threading.Event())

        def invoke_once() -> None:
            try:
                value = operation()
                error = None
            except BaseException as exc:  # turn every provider terminal into a receipt
                value = None
                error = exc
            finished_at = self._clock()
            with invocation.lock:
                invocation.finished = True
                invocation.finished_at = finished_at
                if invocation.settled:
                    invocation.done.set()
                    return
                if deadline is not None and finished_at >= deadline:
                    status, reason = TIMEOUT, "deadline exceeded"
                    value = None
                else:
                    status, reason = self._classify(value, error)
                self._settle(
                    invocation,
                    resolved_call_id,
                    tier,
                    status,
                    value=value,
                    error=error,
                    reason=reason,
                )
                invocation.done.set()

        if deadline is None:
            invoke_once()
            return GuardResult(
                allowed=True,
                status=invocation.status or UNKNOWN_OUTCOME,
                call_id=resolved_call_id,
                tier=tier,
                value=invocation.value,
                error=invocation.error,
                reason=invocation.reason,
                receipt=invocation.receipt,
            )

        worker = threading.Thread(
            target=invoke_once,
            name=f"honcho-paid-reasoning-{resolved_call_id[:12]}",
            daemon=True,
        )
        try:
            worker.start()
        except BaseException as exc:
            with invocation.lock:
                self._settle(
                    invocation,
                    resolved_call_id,
                    tier,
                    FAILURE,
                    error=exc,
                    reason="paid-call worker could not start",
                )
                invocation.done.set()
            return GuardResult(
                allowed=True,
                status=invocation.status or UNKNOWN_OUTCOME,
                call_id=resolved_call_id,
                tier=tier,
                error=invocation.error,
                reason=invocation.reason,
                receipt=invocation.receipt,
                worker_thread=None,
            )

        wait_for = max(0.0, deadline - self._clock())
        if invocation.done.wait(wait_for):
            with invocation.lock:
                return GuardResult(
                    allowed=True,
                    status=invocation.status or UNKNOWN_OUTCOME,
                    call_id=resolved_call_id,
                    tier=tier,
                    value=invocation.value,
                    error=invocation.error,
                    reason=invocation.reason,
                    receipt=invocation.receipt,
                    worker_thread=worker,
                )

        with invocation.lock:
            if not invocation.settled:
                self._settle(
                    invocation,
                    resolved_call_id,
                    tier,
                    TIMEOUT,
                    reason="deadline exceeded",
                )
                invocation.done.set()
            return GuardResult(
                allowed=True,
                status=invocation.status or TIMEOUT,
                call_id=resolved_call_id,
                tier=tier,
                value=invocation.value,
                error=invocation.error,
                reason=invocation.reason,
                receipt=invocation.receipt,
                worker_thread=worker,
            )

    # Naming aliases keep the one-call seam readable at each provider callsite.
    run = execute
    call_once = execute
    reserve_and_call = execute


# Provider-oriented aliases.
HonchoReasoningGuard = PaidReasoningGuard
OneCallReasoningGuard = PaidReasoningGuard
AtomicReservationStore = InMemoryReservationStore
RecordingReceiptSink = InMemoryReceiptSink
make_trusted_tool_call_id = issue_trusted_tool_call_id
make_explicit_approval = issue_explicit_approval


__all__ = [
    "AtomicReservationStore",
    "DENIED",
    "FAILURE",
    "HIGH",
    "HonchoReasoningGuard",
    "INTERACTIVE",
    "INTERACTIVE_TIERS",
    "InMemoryReceiptSink",
    "InMemoryReservationStore",
    "LOW",
    "MAX",
    "MEDIUM",
    "MINIMAL",
    "OFFLINE",
    "OfflineManifestEntry",
    "OfflineReasoningManifest",
    "OneCallReasoningGuard",
    "PaidReasoningGuard",
    "PolicyDecision",
    "ReasoningReceipt",
    "RecordingReceiptSink",
    "SUCCESS",
    "TIMEOUT",
    "UNKNOWN_OUTCOME",
    "UnknownOutcomeError",
    "UnknownOutcomeException",
    "VALID_MODES",
    "VALID_TIERS",
    "GuardResult",
    "is_trusted_tool_call_id",
    "issue_explicit_approval",
    "issue_trusted_tool_call_id",
    "make_explicit_approval",
    "make_trusted_tool_call_id",
    "validate_reasoning_request",
    "validate_tier",
]
