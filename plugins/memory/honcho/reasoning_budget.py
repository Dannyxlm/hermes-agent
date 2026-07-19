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
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
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


def derive_trusted_tool_call_id(
    secret: bytes | bytearray | str,
    *,
    session_id: str,
    turn_id: str,
    tool_name: str,
    runtime_tool_call_id: str,
) -> str:
    """Derive a stable opaque reservation ID for one runtime tool invocation."""
    key = _secret_bytes(secret)
    components = tuple(
        str(value or "").strip()
        for value in (session_id, turn_id, tool_name, runtime_tool_call_id)
    )
    if any(not value or len(value) > 4096 for value in components):
        raise ValueError("complete bounded runtime tool identity is required")
    payload = bytearray(b"honcho-reasoning-reservation-v1\0")
    for component in components:
        encoded = component.encode("utf-8")
        payload.extend(len(encoded).to_bytes(4, "big"))
        payload.extend(encoded)
    nonce = hmac.new(key, bytes(payload), hashlib.sha256).digest()[:24]
    nonce_part = _b64(nonce)
    body = f"{_CALL_ID_PREFIX}.{nonce_part}".encode("ascii")
    signature = _b64(hmac.new(key, body, hashlib.sha256).digest())
    return f"{body.decode('ascii')}.{signature}"


def load_private_reasoning_key(path: str) -> bytes | None:
    """Read one stable mode-private 32-byte key without following symlinks."""
    try:
        if not isinstance(path, str) or not os.path.isabs(path):
            return None
        parent = os.path.dirname(path)
        if os.path.realpath(parent) != os.path.abspath(parent):
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_mode & 0o077
                or info.st_size != 32
            ):
                return None
            value = os.read(fd, 33)
            return value if len(value) == 32 else None
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError):
        return None


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


class DurableFileReservationStore:
    """Process-shared append-only reservation ledger; reservations never release."""

    _SCHEMA = "honcho-reasoning-reservation-v1"
    _MAX_BYTES = 16 * 1024 * 1024

    def __init__(self, path: str) -> None:
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ValueError("reservation ledger path must be absolute")
        self.path = path

    def _open(self) -> int:
        parent = os.path.dirname(self.path)
        if os.path.realpath(parent) != os.path.abspath(parent):
            raise OSError("reservation ledger parent must not contain symlinks")
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_APPEND
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = os.open(self.path, flags, 0o600)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size > self._MAX_BYTES
        ):
            os.close(fd)
            raise OSError("unsafe reservation ledger")
        return fd

    def _entries_locked(self, fd: int) -> dict[str, str]:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = self._MAX_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > self._MAX_BYTES:
            raise OSError("reservation ledger exceeds size limit")
        entries: dict[str, str] = {}
        for raw_line in payload.splitlines():
            if not raw_line:
                continue
            value = json.loads(raw_line)
            if (
                not isinstance(value, dict)
                or set(value) != {"schema", "call_id", "tier", "reserved_at"}
                or value.get("schema") != self._SCHEMA
                or value.get("tier") not in VALID_TIERS
                or not isinstance(value.get("call_id"), str)
                or not isinstance(value.get("reserved_at"), str)
            ):
                raise OSError("reservation ledger entry is invalid")
            call_id = value["call_id"]
            tier = value["tier"]
            if call_id in entries and entries[call_id] != tier:
                raise OSError("reservation ledger contains a conflicting duplicate")
            entries[call_id] = tier
        return entries

    def reserve(self, call_id: str, tier: str) -> bool:
        fd = self._open()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            entries = self._entries_locked(fd)
            if call_id in entries:
                return False
            value = {
                "schema": self._SCHEMA,
                "call_id": call_id,
                "tier": tier,
                "reserved_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            line = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
            if os.fstat(fd).st_size + len(line) > self._MAX_BYTES:
                raise OSError("reservation ledger exceeds size limit")
            view = memoryview(line)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short reservation ledger write")
                view = view[written:]
            os.fsync(fd)
            directory_fd = os.open(os.path.dirname(self.path), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return True
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def contains(self, call_id: str) -> bool:
        fd = self._open()
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            return call_id in self._entries_locked(fd)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def reservations(self) -> list[OfflineManifestEntry]:
        fd = self._open()
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            entries = self._entries_locked(fd)
            return [OfflineManifestEntry(call_id, tier) for call_id, tier in entries.items()]
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


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
        on_reserved: Callable[[], bool | None] | None = None,
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

        if on_reserved is not None:
            try:
                write_ahead_persisted = on_reserved()
            except Exception:
                write_ahead_persisted = False
            if write_ahead_persisted is False:
                return self._denied(
                    resolved_call_id,
                    tier,
                    "reservation receipt could not be persisted",
                )

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


PUBLIC_REASONING_RECEIPT_SCHEMA = "honcho-reasoning-receipt-v1"
_PUBLIC_TERMINAL_STATUSES = frozenset(("succeeded", "failed", "timed_out", "unknown_outcome"))


def build_public_reasoning_receipt(
    *,
    tool_receipt: Any,
    reasoning_call_id: str,
    phase: str,
    status: str,
    tier: str,
    estimated_cost_usd: float,
    error_class: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Build a content-free CloudSeed receipt from sealed executor HMACs."""
    if phase not in {"reserved", "terminal"}:
        raise ValueError("invalid reasoning receipt phase")
    if phase == "reserved" and status != "reserved":
        raise ValueError("reservation receipt must have reserved status")
    if phase == "terminal" and status not in _PUBLIC_TERMINAL_STATUSES:
        raise ValueError("invalid terminal reasoning status")
    if tier not in VALID_TIERS:
        raise ValueError("invalid reasoning tier")
    timestamp = observed_at or dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    opaque_call_id = hashlib.sha256(str(reasoning_call_id).encode("utf-8")).hexdigest()
    receipt: dict[str, Any] = {
        "schema_version": PUBLIC_REASONING_RECEIPT_SCHEMA,
        "receipt_id": f"rr:{phase}:{opaque_call_id[:20]}:{secrets.token_hex(8)}",
        "reasoning_call_id": opaque_call_id,
        "phase": phase,
        "status": status,
        "tier": tier,
        "profile": str(getattr(tool_receipt, "profile", "") or ""),
        "session_hmac": str(getattr(tool_receipt, "session_hmac", "") or ""),
        "turn_hmac": str(getattr(tool_receipt, "turn_binding_hmac", "") or ""),
        "tool_call_hmac": str(getattr(tool_receipt, "tool_call_hmac", "") or ""),
        "policy_revision": str(getattr(tool_receipt, "policy_revision", "") or ""),
        "writer_release": str(getattr(tool_receipt, "writer_release", "") or ""),
        "estimated_cost_usd": max(0.0, float(estimated_cost_usd)),
        "observed_at": timestamp,
        "content_free": True,
    }
    if error_class:
        cleaned = "".join(
            char for char in str(error_class)
            if char.isalnum() or char in "._:-"
        )[:96]
        if cleaned:
            receipt["error_class"] = cleaned
    if not validate_public_reasoning_receipt(receipt):
        raise ValueError("invalid public reasoning receipt")
    return receipt


def validate_public_reasoning_receipt(receipt: Mapping[str, Any]) -> bool:
    required = {
        "schema_version", "receipt_id", "reasoning_call_id", "phase", "status",
        "tier", "profile", "session_hmac", "turn_hmac", "tool_call_hmac",
        "policy_revision", "writer_release", "estimated_cost_usd", "observed_at",
        "content_free",
    }
    allowed = required | {"error_class", "offline_manifest_sha256", "explicit_approval_id"}
    if not isinstance(receipt, Mapping):
        return False
    if not required.issubset(receipt) or not set(receipt).issubset(allowed):
        return False
    if receipt.get("schema_version") != PUBLIC_REASONING_RECEIPT_SCHEMA:
        return False
    if receipt.get("content_free") is not True:
        return False
    for key in ("reasoning_call_id", "session_hmac", "turn_hmac", "tool_call_hmac"):
        value = receipt.get(key)
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            return False
    if receipt.get("phase") == "reserved":
        if receipt.get("status") != "reserved":
            return False
    elif receipt.get("phase") == "terminal":
        if receipt.get("status") not in _PUBLIC_TERMINAL_STATUSES:
            return False
    else:
        return False
    if receipt.get("tier") not in VALID_TIERS:
        return False
    if receipt.get("tier") in {HIGH, MAX} and "offline_manifest_sha256" not in receipt:
        return False
    if receipt.get("tier") == MAX and "explicit_approval_id" not in receipt:
        return False
    if not isinstance(receipt.get("profile"), str) or not receipt.get("profile"):
        return False
    if not isinstance(receipt.get("policy_revision"), str) or not receipt.get("policy_revision"):
        return False
    if not isinstance(receipt.get("writer_release"), str) or not receipt.get("writer_release"):
        return False
    cost = receipt.get("estimated_cost_usd")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not 0 <= cost <= 1000:
        return False
    try:
        dt.datetime.fromisoformat(str(receipt.get("observed_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    receipt_id = receipt.get("receipt_id")
    return bool(
        isinstance(receipt_id, str)
        and 8 <= len(receipt_id) <= 160
        and all(c.isalnum() or c in "._:-" for c in receipt_id)
    )


def append_public_reasoning_receipt(
    path: str,
    receipt: Mapping[str, Any],
    *,
    max_bytes: int = 16 * 1024 * 1024,
) -> bool:
    """Append one fsynced JSONL receipt with no-follow and private-mode checks."""
    if not validate_public_reasoning_receipt(receipt):
        return False
    try:
        absolute = os.path.abspath(str(path or ""))
        if not os.path.isabs(str(path or "")) or absolute != str(path):
            return False
        parent = os.path.dirname(absolute)
        if os.path.realpath(parent) != parent or not os.path.isdir(parent):
            return False
        if os.path.lexists(absolute) and stat.S_ISLNK(os.lstat(absolute).st_mode):
            return False
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(absolute, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return False
            if stat.S_IMODE(info.st_mode) & 0o077:
                return False
            payload = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            if info.st_size + len(payload) > max_bytes:
                return False
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                if os.fstat(fd).st_size + len(payload) > max_bytes:
                    return False
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        return False
                    view = view[written:]
                os.fsync(fd)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return True
    except (OSError, TypeError, ValueError):
        return False


__all__ = [
    "AtomicReservationStore",
    "DENIED",
    "DurableFileReservationStore",
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
    "PUBLIC_REASONING_RECEIPT_SCHEMA",
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
    "derive_trusted_tool_call_id",
    "issue_explicit_approval",
    "issue_trusted_tool_call_id",
    "load_private_reasoning_key",
    "append_public_reasoning_receipt",
    "build_public_reasoning_receipt",
    "make_explicit_approval",
    "make_trusted_tool_call_id",
    "validate_reasoning_request",
    "validate_public_reasoning_receipt",
    "validate_tier",
]
