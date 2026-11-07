"""Serve -> owner cutover for one typed prompt (RT-37, #140385).

A legacy ``hermes serve`` backend (no session authority) refuses a turn while the gateway's durable
ledger holds work for the same stored session (``session_lifecycle._canonical_ledger_refusal``): a
legacy turn would run beside the canonical FIFO and write the transcript twice. A Bot Chat the
gateway also owns hits that on every gateway turn, so the user's own Desktop prompt was refused
``SESSION_NOT_OWNED`` and the card offered "Start new session".

The #106742 contract is one owner and one FIFO, with clients that submit. So the prompt goes to the
owner instead: this backend dials the ready gateway serving the session's profile home, attaches to
the session and admits the prompt into its durable FIFO (``prompt.submit`` with a fresh submission
id). The owner runs it after the current turn, exactly once; nothing runs here. The owner's frames
for THAT admission are relayed onto the Desktop's live session, so the client sees an ordinary
queued send that later streams and completes. Relay loss is an observation failure, never proof
of completion. Control identity remains on the live session after the relay exits; recovery reads
the owner's receipt and never resubmits an unknown input.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Ledger states a forwarded prompt may wait behind: they end on their own.
FORWARDABLE_LEDGER_STATES = frozenset({"started", "queued", "registered", "running"})
#: How long ``prompt.submit`` waits for the owner's admission receipt.
ADMIT_TIMEOUT_S = 30.0
#: Ceiling on one relay; the owner keeps the work either way (it is durable).
RELAY_TIMEOUT_S = 6 * 3600.0
#: How often an idle relay re-checks that its Desktop session still exists: a closed session
#: releases the thread and the owner socket without waiting for the owner's next frame.
RELAY_FINALIZE_POLL_S = 1.0
#: Owner turn frames relayed for the forwarded admission. Not its deltas: a Desktop turn that streamed
#: nothing re-reads the stored transcript at ``message.complete``, which brings the user's own row
#: (written by the owner, not by this backend) and the reply in their committed order.
_RELAYED = frozenset({"message.start", "message.complete"})
#: The owner's ``prompt.submit`` refusals raised before its durable write (dispatcher, ``authorize``,
#: ``submission``, the admission gate, and the ledger's own checks inside the rolled-back write): only
#: these prove nothing will run. ``storage_unavailable`` and an internal error can also come from the
#: pending publication AFTER the commit (``SessionAuthority._admitted``), so they are never listed.
#: ``not_found`` can follow the commit too, when the session is deleted meanwhile (``_handle``), but a
#: session row is deleted only with its admissions (``ON DELETE RESTRICT``), so that one never runs.
_REFUSED_BEFORE_COMMIT = frozenset({
    "invalid_params", "runtime_draining", "runtime_retired", "profile_mismatch", "permission_denied",
    "not_found", "stale_epoch", "admission_conflict", "queue_full"})


class OwnerUnavailable(RuntimeError):
    """The owner could not take the prompt; ``str(exc)`` names why for the refusal."""


class OwnerNotRunning(OwnerUnavailable):
    """No ready gateway serves the home: nothing will end the ledger's work on its own."""


class OwnerUnconfirmed(OwnerUnavailable):
    """The owner's admission receipt did not arrive in time. The relay keeps going, so the prompt
    may still be (or already be) queued there: never "nothing was sent", never a retry."""


def discover_owner(home: Path):
    """The ready gateway endpoint serving ``home``. Discovery only: never starts one."""
    from hermes_cli.gateway_runtime import discover_gateway_endpoint
    discovery = discover_gateway_endpoint(home, timeout=5)
    if discovery.state != "ready" or discovery.endpoint is None:
        raise OwnerNotRunning(f"the gateway that owns it is {discovery.state}")
    return discovery.endpoint


@contextlib.asynccontextmanager
async def connect_owner(home: Path, endpoint, *, scope=None, max_size: int = 2 ** 20):
    """A ``GatewayClient`` on ``endpoint``, the ready gateway serving ``home``; ``scope``: the
    ticket's profile scope (``"host"``: the one native Desktop holds); ``max_size``: the largest
    answer frame accepted (websockets' 1 MiB default)."""
    from hermes_cli.gateway_client import GATEWAY_WS_PROTOCOL, GatewayClient, _session_ticket, gateway_ws_target
    from websockets.asyncio.client import connect

    ticket = await asyncio.to_thread(_session_ticket, home, endpoint, scope=scope)
    url, protocols = gateway_ws_target(endpoint, ticket)
    # Loopback owner dial: never through HTTP(S)_PROXY (websockets>=14 honours it by default).
    async with connect(url, subprotocols=protocols, open_timeout=10, proxy=None, max_size=max_size) as ws:
        if ws.subprotocol != GATEWAY_WS_PROTOCOL:
            raise OwnerUnavailable("the gateway answered with another protocol")
        async with GatewayClient(ws) as client:
            yield client


def forwardable(refusal: Any, session: dict, text: Any, params: dict) -> bool:
    """Whether a ledger refusal of this ``prompt.submit`` can go to the owner instead."""
    if getattr(refusal, "ledger_status", None) not in FORWARDABLE_LEDGER_STATES:
        return False
    return (isinstance(text, str) and bool(text.strip()) and not session.get("attached_images")
            and not any(params.get(key) is not None for key in (
                "truncate_before_user_ordinal", "truncate_before_row_id", "truncate_before_message_id")))


def _payload_for(event_type: str, payload: Any) -> dict | None:
    """The owner payload projected onto this wire's contract for ``event_type`` (None: drop it)."""
    from tui_gateway.contracts.registry import EVENTS
    contract = EVENTS.get(event_type)
    if contract is None or contract.payload is None or not isinstance(payload, dict):
        return None
    shaped = {key: value for key, value in payload.items() if key in contract.payload.model_fields}
    from pydantic import ValidationError
    try:
        contract.payload.model_validate(shaped)
    except ValidationError:
        if event_type != "message.complete":
            return None
        shaped = {"text": payload.get("text") if isinstance(payload.get("text"), str) else ""}
    return shaped


class _Relay:
    def __init__(self, sid: str, session: dict, key: str, home: Path, endpoint, text: str, emit):
        self.sid, self.session, self.key, self.home, self.text, self.emit = sid, session, key, home, text, emit
        self.endpoint, self.listed = endpoint, None
        self.admitted: Future = Future()
        self.request_id = "desktop-forward-" + uuid.uuid4().hex

    def run(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as exc:  # health: allow BLE001 -- thread boundary: handed to the waiting submit or logged with traceback
            if not self.admitted.done():
                self.admitted.set_exception(exc)
            else:
                logger.warning("owner relay for %s ended: %s", self.key, exc, exc_info=True)

    async def _run(self) -> None:
        async with connect_owner(self.home, self.endpoint) as client:
            attached = await client.rpc("session.resume", session_id=self.key, omit_messages=True)
            target = str(attached.get("session_id") or self.key)
            receipt = await self._submit(client, target)
            if receipt.get("voice_stopped") is True:
                # TUI-26: the owner took a typed stop phrase as the end of its LOCAL voice chat and
                # admitted nothing: there is no turn to relay.
                self.admitted.set_result(receipt)
                return
            admission_id = receipt.get("admission_id")
            if not isinstance(admission_id, str) or receipt.get("status") not in ("queued", "started", "terminal"):
                raise OwnerUnavailable("the gateway did not admit it")
            with self.session["history_lock"]:
                self.session.setdefault("_forwarded_admissions", {})[admission_id] = {
                    "home": self.home, "endpoint": self.endpoint, "target": target,
                    "receipt": receipt, "user": self.text, "input_id": self.request_id}
            self.admitted.set_result(receipt)
            logger.info("forwarded Desktop prompt for %s to the owner FIFO: admission=%s",
                        self.key, admission_id)
            await self._relay(client, admission_id)

    async def _submit(self, client, target: str) -> dict:
        """``prompt.submit`` on the owner. Once the request is handed to the socket, only a received
        refusal the owner raises before its write (``_REFUSED_BEFORE_COMMIT``) proves nothing was
        queued: a lost connection, a missing answer or any other error may follow the owner's durable
        commit (a gateway restart mid-forward, a storage error publishing the pending list), so it is
        unconfirmed, never "nothing was sent" (a resend would run a second copy)."""
        from hermes_cli.gateway_client import GatewayRPCError
        try:
            return await client.rpc("prompt.submit", session_id=target, text=self.text,
                                    submission_id=self.request_id)
        except Exception as exc:
            # The owner names its refusal in ``data.reason``; ``message`` may be a display string.
            reason = exc.data.get("reason", str(exc)) if isinstance(exc, GatewayRPCError) else None
            if isinstance(reason, str) and reason in _REFUSED_BEFORE_COMMIT:
                raise
            raise OwnerUnconfirmed("the gateway did not confirm it") from exc

    def _pending(self, params: dict, admission_id: str) -> None:
        """Preserve pending execution state; expose only controls this bridge actually serves."""
        rows = (params.get("payload") or {}).get("pending")
        if not isinstance(rows, list):
            return
        with self.session["history_lock"]:
            record = self.session.get("_forwarded_admissions", {}).get(admission_id)
            for row in rows:
                if isinstance(row, dict) and row.get("admission_id") == admission_id and record is not None:
                    record["receipt"] = row
        listed = pending_submissions(self.session)
        if listed != self.listed:
            self.listed = listed
            self.emit("session.info", self.sid, {"stored_session_id": self.key, "pending_submissions": listed})

    async def _relay(self, client, admission_id: str) -> None:
        settled = False
        try:
            async with asyncio.timeout(RELAY_TIMEOUT_S):
                while not settled and not self.session.get("_finalized"):
                    try:
                        async with asyncio.timeout(RELAY_FINALIZE_POLL_S):
                            frame = await client.events.get()
                    except TimeoutError:
                        continue
                    if isinstance(frame, BaseException):
                        return
                    params = frame.get("params") if isinstance(frame, dict) else None
                    if not isinstance(params, dict):
                        continue
                    if params.get("type") == "session.info":
                        self._pending(params, admission_id)
                        continue
                    if params.get("admission_id") != admission_id:
                        continue
                    event_type = params.get("type")
                    if event_type not in _RELAYED:
                        continue
                    payload = _payload_for(event_type, params.get("payload"))
                    if payload is None:
                        continue
                    self.emit(event_type, self.sid, payload)
                    settled = event_type == "message.complete"
                    if settled:
                        with self.session["history_lock"]:
                            record = self.session.get("_forwarded_admissions", {}).get(admission_id)
                            if record is not None:
                                record["receipt"] = {**record["receipt"], "status": "terminal"}
                        self.emit("session.info", self.sid, {"stored_session_id": self.key,
                            "pending_submissions": pending_submissions(self.session)})
        finally:
            if not settled and not self.session.get("_finalized"):
                self.emit("session.info", self.sid, {"stored_session_id": self.key,
                    "pending_submissions": pending_submissions(self.session), "owner_connected": False})


def blocking_unknown(session: dict, refusal=None) -> list[dict]:
    """Refresh the ledger's exact barrier through the profile-bound owner's receipt RPC.

    A local ledger ID is only a lookup hint, never control authority. No attach (which would
    read pending bodies), submit, or recovery write occurs. Offline/old/unentitled owners
    cannot advertise Discard. Observed barriers share the existing receipt/control cache,
    but are not this window's submitted prompts and ordinary Stop must not touch them.
    """
    from tui_gateway import server
    from tui_gateway.contracts.common import BlockingUnknownAdmission
    from hermes_cli.gateway_client import GatewayClientError
    from hermes_state_runtime import RuntimeStoreError
    from pydantic import ValidationError
    from websockets.exceptions import WebSocketException

    refusal = refusal if refusal is not None else server._canonical_ledger_refusal(session)
    aid = getattr(refusal, 'ledger_admission_id', None)
    with session['history_lock']:
        records = session.get('_forwarded_admissions', {})
        for old in [key for key, row in records.items() if row.get('observed_only')]:
            del records[old]
    if getattr(refusal, 'ledger_status', None) != 'unknown' or not aid:
        return []  # A worker-only barrier has no admission control on this bridge.
    home = Path(session.get('profile_home') or server._hermes_home).resolve()
    target = str(session['session_key'])

    async def refresh():
        endpoint = discover_owner(home)
        async with connect_owner(home, endpoint) as client:
            receipt = await client.rpc('prompt.receipt', session_id=target, admission_id=aid)
        if (receipt.get('admission_id') != aid or receipt.get('ref') != {
                'session_id': target, 'profile_id': endpoint.profile_id}):
            raise RuntimeStoreError('profile_mismatch')
        shaped = BlockingUnknownAdmission.model_validate({
            key: receipt.get(key, [] if key == 'controls' else None) for key in (
                'admission_id', 'ref', 'status', 'execution_generation', 'controls')}).model_dump()
        with session['history_lock']:
            records = session.setdefault('_forwarded_admissions', {})
            if aid not in records:
                records[aid] = {'home': home, 'endpoint': endpoint, 'target': target,
                                'receipt': receipt, 'observed_only': True}
            else:
                records[aid]['receipt'] = receipt
        return [shaped]
    async def refresh_bounded():
        async with asyncio.timeout(ADMIT_TIMEOUT_S):
            return await refresh()
    try:
        return asyncio.run(refresh_bounded())
    except (GatewayClientError, RuntimeStoreError, ValidationError, OSError, RuntimeError,
            TimeoutError, WebSocketException):
        logger.debug('Owner could not verify unknown admission for %s', target, exc_info=True)
        return []


def cached_blocking_unknown(session: dict) -> list[dict]:
    """Project only verified receipts retained by the existing owner control path."""
    from tui_gateway import server
    home = Path(session.get('profile_home') or server._hermes_home).resolve()
    with session['history_lock']:
        return [{key: row['receipt'][key] for key in (
                    'admission_id', 'ref', 'status', 'execution_generation')}
                | {'controls': row['receipt'].get('controls') or []}
                for row in session.get('_forwarded_admissions', {}).values()
                if row.get('observed_only') and row['home'] == home
                and row['target'] == session.get('session_key') and row['receipt'].get('status') == 'unknown']


def pending_submissions(session: dict) -> list[dict]:
    """Session-owned identity, not relay-owned state: safe to read after socket loss."""
    controls = {"queued": ["cancel"], "started": ["interrupt"], "unknown": ["resolve_unknown"]}
    with session["history_lock"]:
        return [{"admission_id": aid, "status": r["receipt"]["status"], "user": r["user"],
                 "input_id": r["input_id"], "execution_generation": r["receipt"].get("execution_generation"),
                 "controls": controls[r["receipt"]["status"]]}
                for aid, r in session.get("_forwarded_admissions", {}).items()
                if not r.get('observed_only') and r["receipt"].get("status") in controls]


def control_forwarded(session: dict, method: str, admission_id: str, generation=None, *, stop=False) -> dict:
    """Reattach to the same profile and read the exact admission before controlling it.

    Discovery may find a replacement gateway, but cannot select another home or another admission.
    The owner's cancel handles queued→claimed races; Stop/Discard always carry the observed generation.
    """
    from tui_gateway import server
    from hermes_state_runtime import RuntimeStoreError
    with session["history_lock"]:
        record = session.get("_forwarded_admissions", {}).get(admission_id)
        if record is None:
            raise RuntimeStoreError("not_found")
        home = Path(session.get("profile_home") or server._hermes_home).resolve()
        if home != record["home"]:
            raise RuntimeStoreError("profile_mismatch")
        target, expected_ref = record["target"], record["receipt"].get("ref")
        if record.get('observed_only') and target != session.get('session_key'):
            raise RuntimeStoreError('not_found')

    async def call():
        endpoint = discover_owner(home)
        async with connect_owner(home, endpoint) as client:
            receipt = await client.rpc("prompt.receipt", session_id=target, admission_id=admission_id)
            if receipt.get("ref") != expected_ref:
                raise RuntimeStoreError("profile_mismatch")
            with session["history_lock"]:
                record["endpoint"], record["receipt"] = endpoint, receipt
            if method == "prompt.receipt":
                return receipt
            if record.get('observed_only') and (stop or method != 'prompt.resolve_unknown'
                    or 'resolve_unknown' not in (receipt.get('controls') or [])):
                raise RuntimeStoreError('permission_denied')
            verb = method
            if stop:
                if receipt["status"] == "terminal":
                    return receipt
                if receipt["status"] == "unknown":
                    raise RuntimeStoreError("unknown_execution")  # Stop is never human Discard.
                verb = "session.interrupt" if receipt["status"] == "started" else "prompt.cancel"
            params = {"session_id": target}
            if verb != "session.interrupt":
                params["admission_id"] = admission_id
            if verb in ("session.interrupt", "prompt.resolve_unknown"):
                observed = receipt.get("execution_generation")
                if generation is not None and generation != observed:
                    raise RuntimeStoreError("stale_generation")
                if observed is None or receipt["status"] not in ("started", "unknown"):
                    raise RuntimeStoreError("stale_generation")
                params["execution_generation"] = observed
            result = await client.rpc(verb, **params)
            refreshed = await client.rpc("prompt.receipt", session_id=target, admission_id=admission_id)
            with session["history_lock"]:
                record["receipt"] = refreshed
            return result
    return asyncio.run(call())


def forward_rpc(home: Path, endpoint, method: str, params: dict) -> Any:
    """One request/answer RPC on the owner serving ``home`` (Group Chat calls, WA-19), on the
    host-scoped grant native Desktop itself holds, so a room may seat the served sibling profiles."""
    from gateway.hosted_room_attachments import MAX_ATTACHMENT_BYTES
    # A groups.attachment.download answer carries a whole file as base64: room for the largest
    # one the room store accepts, plus its JSON-RPC envelope.
    frame = (MAX_ATTACHMENT_BYTES + 2) // 3 * 4 + 2 ** 20

    async def call():
        async with connect_owner(home, endpoint, scope="host", max_size=frame) as client:
            return await client.rpc(method, **params)
    return asyncio.run(call())


def forward_prompt(sid: str, session: dict, text: str, *, emit=None, home: Path | None = None) -> dict:
    """Admit ``text`` into the owner's FIFO for this session; the owner's receipt.

    Raises ``OwnerNotRunning`` when no gateway serves the home, ``OwnerUnavailable`` (or the
    client's refusal) when the owner did not take it, and ``OwnerUnconfirmed`` when the prompt may
    be queued there: the receipt timed out, or the owner was lost or failed after the request was sent."""
    from tui_gateway import server
    key = str(session.get("session_key") or "")
    if not key:
        raise OwnerUnavailable("the chat has no stored session yet")
    home = Path(home or session.get("profile_home") or server._hermes_home).resolve()
    relay = _Relay(sid, session, key, home, discover_owner(home), text, emit or server._emit)
    from agent.memory_provider import spawn_context_thread
    spawn_context_thread(relay.run, name=f"owner-forward-{key[:24]}").start()
    try:
        return relay.admitted.result(timeout=ADMIT_TIMEOUT_S)
    except FutureTimeout as exc:
        raise OwnerUnconfirmed("the gateway did not confirm it in time") from exc
