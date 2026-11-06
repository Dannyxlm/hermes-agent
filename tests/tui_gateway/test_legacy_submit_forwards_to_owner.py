"""RT-37 (#140385): a Desktop prompt on a session whose gateway owner is running a turn (a Bot Chat
delivery) is admitted into the OWNER's durable FIFO and runs after that turn, exactly once. It is not
refused ``SESSION_NOT_OWNED``, nothing runs in the legacy backend, and the Desktop gets a queued
receipt, then the owner's start and completion for that admission.

Real: the SessionDB, the gateway turn's started admission (``admit_session_input`` +
``claim_session_input``), the legacy ``prompt.submit`` and its ledger gate, and the owner
(``SessionAuthority`` + ``AuthorityConnection``: attach, admission, claim stamp, settlement and
publication). Replaced: only the owner's discovery and socket dial (``owner_forward.discover_owner`` /
``connect_owner``), by an in-process client speaking to the same ``AuthorityConnection``."""
import asyncio
import contextlib
import threading
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from hermes_state_runtime import (
    admit_session_input, begin_runtime_epoch, claim_session_input, list_session_admissions,
    recover_session_inputs, settle_session_input,
)
from tui_gateway import server
from tests.waiting import wait_until


@pytest.mark.parametrize("action", ["cancel", "stop", "discard", "foreign", "claim_race"])
def test_forwarded_controls_reach_only_the_exact_owner_admission(monkeypatch, tmp_path, action):
    from gateway.session_contract import SessionRef
    from tui_gateway import owner_forward
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key, turns, emitted = _legacy(monkeypatch, db)
    monkeypatch.setattr(server, "_tts_stream_stop", lambda: None)
    monkeypatch.setattr(server, "_resume_wake_after_interrupt", lambda: None)
    try:
        db.create_session(key, "test")
        epoch = begin_runtime_epoch(db, instance_id="gateway")
        bot = admit_session_input(db, epoch=epoch, principal_id="bot", session_id=key,
                                  request_id="dm", payload={"text": "DM"})
        claimed = claim_session_input(db, epoch=epoch, session_id=key)
        authority = _owner(monkeypatch, db, key, epoch)
        reply = server.handle_request({"id": "p", "method": "prompt.submit",
                                       "params": {"session_id": sid, "text": "my question"}})
        aid = reply["result"]["admission_id"]
        session = server._sessions[sid]
        wait_until(lambda: any(e == "session.info" for e, *_ in emitted), timeout=10)
        if action in ("stop", "discard"):
            settle_session_input(db, epoch=epoch, admission_id=bot["admission_id"],
                                 generation=claimed["generation"], outcome="completed")
            row = claim_session_input(db, epoch=epoch, session_id=key)
            ref = SessionRef("owned", key)
            authority._stamp_claim(ref, authority.sessions[key], row)
            wait_until(lambda: owner_forward.pending_submissions(session)[0]["status"] == "started", timeout=10)
        method, params = "prompt.cancel", {"session_id": sid, "admission_id": aid}
        if action == "stop":
            method = "session.interrupt"
            params["execution_generation"] = row["generation"]
        elif action == "discard":
            authority.epoch = begin_runtime_epoch(db, instance_id="gateway-2")
            recover_session_inputs(db, epoch=authority.epoch)
            receipt = server.handle_request({"id": "r", "method": "prompt.receipt", "params": params})
            assert receipt["result"]["status"] == "unknown", receipt
            assert owner_forward.pending_submissions(session)[0]["controls"] == ["resolve_unknown"]
            method = "prompt.resolve_unknown"
            params["execution_generation"] = row["generation"]
            stale_discard = server.handle_request({'id': 'stale-discard', 'method': method,
                'params': {**params, 'execution_generation': row['generation'] - 1}})
            assert stale_discard['error']['data']['reason'] == 'stale_generation'
            assert owner_forward.pending_submissions(session)[0]['status'] == 'unknown'
        elif action == "foreign":
            session["profile_home"] = str(tmp_path / "foreign")
        raced = []
        if action == "claim_race":
            cancel = authority.cancel_or_claimed

            async def claim_after_the_owner_read_queued(actor, ref, admission_id):
                settle_session_input(db, epoch=epoch, admission_id=bot["admission_id"],
                                     generation=claimed["generation"], outcome="completed")
                running = claim_session_input(db, epoch=epoch, session_id=key)
                authority._stamp_claim(ref, authority.sessions[key], running)
                raced.append(running)
                return await cancel(actor, ref, admission_id)
            monkeypatch.setattr(authority, "cancel_or_claimed", claim_after_the_owner_read_queued)
        result = server.handle_request({"id": "control", "method": method, "params": params})
        if raced:
            row = raced[0]
        current = {r["admission_id"]: r for r in list_session_admissions(db, session_id=key, pending_only=False)}
        if action == "foreign":
            assert result["error"]["data"]["reason"] == "profile_mismatch"
            assert current[aid]["status"] == "queued"
        elif action in ("stop", "claim_race"):
            assert "error" not in result, result
            assert authority.pending_stops[key] == row["generation"]
            # A stale Stop cannot cancel the successor generation.
            stale = server.handle_request({"id": "stale", "method": "session.interrupt",
                "params": {"session_id": sid, "admission_id": aid, "execution_generation": row["generation"] - 1}})
            assert stale["error"]["data"]["reason"] == "stale_generation"
        else:
            assert "error" not in result, result
            assert current[aid]["status"] == "terminal"
            assert current[aid]["outcome"] == ("interrupted" if action == "discard" else "cancelled")
        assert turns == []
        assert len(current) == 2
    finally:
        server._sessions[sid]["_finalized"] = True
        server._sessions.pop(sid, None)
        db.close()


def test_relay_loss_is_not_an_owner_completion():
    from tui_gateway import owner_forward
    session, emitted = {"history_lock": threading.RLock()}, []
    relay = owner_forward._Relay("ui", session, "key", None, None, "hi", lambda *a: emitted.append(a))

    async def scenario():
        events = asyncio.Queue()
        await events.put(ConnectionError("owner lost"))
        await relay._relay(SimpleNamespace(events=events), "a1")
    asyncio.run(scenario())
    assert not any(e == "message.complete" for e, *_ in emitted)
    assert emitted[-1][2]["owner_connected"] is False


@pytest.mark.parametrize('compute', [False, True])
def test_plain_stop_after_relay_loss_reaches_the_running_local_turn(monkeypatch, tmp_path, compute):
    from pathlib import Path
    from tui_gateway import owner_forward
    from tests.tui_gateway.test_queued_prompt_persistence import _busy

    with SessionDB(db_path=tmp_path / 'state.db') as db:
        sid, key, _turns, _emitted = _legacy(monkeypatch, db)
        session, stopped = server._sessions[sid], []
        try:
            session['_forwarded_admissions'] = {'old': {
                'receipt': {'status': 'queued'}, 'home': Path(session.get('profile_home') or server._hermes_home),
                'target': key, 'user': 'old', 'input_id': 'old'}}
            relay = owner_forward._Relay(sid, session, key, None, None, 'old', lambda *a: None)

            async def lost():
                events = asyncio.Queue()
                await events.put(ConnectionError('relay lost'))
                await relay._relay(SimpleNamespace(events=events), 'old')
            asyncio.run(lost())
            session['agent'] = SimpleNamespace(interrupt=lambda *a, **k: stopped.append('inline'))
            _busy(session, 'new local turn')
            monkeypatch.setattr(server, '_session_uses_compute_host', lambda _s: compute)
            monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda: SimpleNamespace(
                interrupt=lambda *a, **k: stopped.append('compute')))
            monkeypatch.setattr(server, '_tts_stream_stop', lambda: None)
            monkeypatch.setattr(server, '_resume_wake_after_interrupt', lambda: None)
            reply = server.handle_request({'id': 'stop', 'method': 'session.interrupt',
                                           'params': {'session_id': sid}})
            assert 'error' not in reply, reply
            assert stopped == ['compute' if compute else 'inline']
        finally:
            server._sessions.pop(sid, None)


@pytest.mark.parametrize('first', ['unknown', 'missing', 'stale_started', 'missing_then_started', 'terminal'])
def test_plain_stop_refreshes_each_receipt_and_does_not_starve_later_inputs(monkeypatch, tmp_path, first):
    from pathlib import Path
    from tui_gateway import owner_forward

    with SessionDB(db_path=tmp_path / 'state.db') as db:
        sid, key, _turns, _emitted = _legacy(monkeypatch, db)
        session = server._sessions[sid]
        try:
            db.create_session(key, 'test')
            epoch = begin_runtime_epoch(db, instance_id='gateway')
            authority = _owner(monkeypatch, db, key, epoch)

            async def admit():
                client = _OwnerClient(authority)
                await client.rpc('session.resume', session_id=key)
                return [await client.rpc('prompt.submit', session_id=key, text=text, submission_id=text)
                        for text in ('first', 'later')]
            receipts = asyncio.run(admit())
            records = {}
            for receipt in receipts:
                records[receipt['admission_id']] = {
                    'receipt': receipt, 'home': Path(session.get('profile_home') or server._hermes_home).resolve(),
                    'target': key, 'user': receipt['admission_id'], 'input_id': receipt['admission_id']}
            session['_forwarded_admissions'] = records
            if first in ('missing', 'missing_then_started'):
                record = records.pop(receipts[0]['admission_id'])
                session['_forwarded_admissions'] = {'missing': record, **records}
            if first in ('missing_then_started', 'terminal'):
                claimed = claim_session_input(db, epoch=epoch, session_id=key)
                settle_session_input(db, epoch=epoch, admission_id=claimed['admission_id'],
                                     generation=claimed['generation'], outcome='completed')
                if first == 'terminal':
                    records[receipts[0]['admission_id']]['receipt'] = {
                        **receipts[0], 'status': 'started', 'execution_generation': claimed['generation']}
                else:
                    from gateway.session_contract import SessionRef
                    running = claim_session_input(db, epoch=epoch, session_id=key)
                    authority._stamp_claim(SessionRef('owned', key), authority.sessions[key], running)
            elif first == 'unknown':
                claim_session_input(db, epoch=epoch, session_id=key)
                authority.epoch = begin_runtime_epoch(db, instance_id='replacement')
                recover_session_inputs(db, epoch=authority.epoch)
            elif first == 'stale_started':
                # The cached receipt says started, but the exact admission has returned to queued.
                records[receipts[0]['admission_id']]['receipt'] = {
                    **receipts[0], 'status': 'started', 'execution_generation': 99}
            monkeypatch.setattr(server, '_tts_stream_stop', lambda: None)
            monkeypatch.setattr(server, '_resume_wake_after_interrupt', lambda: None)
            reply = server.handle_request({'id': 'stop', 'method': 'session.interrupt',
                                           'params': {'session_id': sid}})
            rows = {r['admission_id']: r for r in list_session_admissions(db, session_id=key, pending_only=False)}
            if first == 'missing_then_started':
                assert authority.pending_stops[key] == running['generation'], reply
                assert rows[receipts[1]['admission_id']]['status'] == 'started'
            else:
                assert rows[receipts[1]['admission_id']]['outcome'] == 'cancelled', reply
            if first == 'unknown':
                assert rows[receipts[0]['admission_id']]['status'] == 'unknown'  # only explicit Discard resolves it
                assert authority.pending_stops == {}
                assert owner_forward.pending_submissions(session)[0]['controls'] == ['resolve_unknown']
            elif first == 'stale_started':
                assert 'error' not in reply, reply
                assert rows[receipts[0]['admission_id']]['outcome'] == 'cancelled'
            elif first == 'terminal':
                assert 'error' not in reply, reply
                assert rows[receipts[0]['admission_id']]['outcome'] == 'completed'
        finally:
            server._sessions.pop(sid, None)


def test_forwarded_websocket_failure_is_a_typed_uncertain_control_error(monkeypatch, tmp_path):
    from pathlib import Path
    from websockets.exceptions import ConnectionClosedError
    from tui_gateway import owner_forward

    with SessionDB(db_path=tmp_path / 'state.db') as db:
        sid, key, _turns, _emitted = _legacy(monkeypatch, db)
        session = server._sessions[sid]
        try:
            session['_forwarded_admissions'] = {'a': {
                'receipt': {'status': 'queued'}, 'home': Path(session.get('profile_home') or server._hermes_home).resolve(),
                'target': key, 'user': 'question', 'input_id': 'a'}}
            monkeypatch.setattr(owner_forward, 'discover_owner', lambda _h: 'owner')

            @contextlib.asynccontextmanager
            async def closed(*a, **k):
                raise ConnectionClosedError(None, None)
                yield
            monkeypatch.setattr(owner_forward, 'connect_owner', closed)
            reply = server.handle_request({'id': 'receipt', 'method': 'prompt.receipt',
                'params': {'session_id': sid, 'admission_id': 'a'}})
            assert reply['error']['data']['reason'] == 'SESSION_OWNER_UNCONFIRMED', reply
            assert session['_forwarded_admissions']['a']['receipt']['status'] == 'queued'
        finally:
            server._sessions.pop(sid, None)


class _OwnerClient:
    """``GatewayClient``'s surface (``rpc`` + ``events``) over a real ``AuthorityConnection``."""

    def __init__(self, authority, identity=None):
        from gateway.session_controls import AuthorityConnection
        self.loop, self.events = asyncio.get_running_loop(), asyncio.Queue()
        self.conn = AuthorityConnection(authority, self, identity or {"user_id": "desktop"})

    def write(self, frame):  # the owner's fanout to this socket
        self.loop.call_soon_threadsafe(self.events.put_nowait, frame)
        return True

    def close(self):
        return None

    async def rpc(self, method, **params):
        reply = await self.conn.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in reply:
            from hermes_cli.gateway_client import rpc_error
            raise rpc_error(reply["error"])
        return reply["result"]


def _legacy(monkeypatch, db):
    monkeypatch.setattr(server, "_get_db", lambda: db)
    for name in ("_schedule_agent_build", "_schedule_session_cap_enforcement", "_register_session_cwd",
                 "_start_agent_build"):
        monkeypatch.setattr(server, name, lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *_a: False)
    turns, emitted = [], []
    monkeypatch.setattr(server, "_run_after_agent_ready", lambda *a, **kw: turns.append(a[1]))
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: emitted.append((event, sid, payload)) or True)
    created = server.handle_request({"id": "c", "method": "session.create", "params": {"source": "desktop"}})
    return created["result"]["session_id"], created["result"]["stored_session_id"], turns, emitted


def _owner(monkeypatch, db, key, epoch):
    from gateway.session_authority import LiveSession, SessionAuthority
    authority = SessionAuthority(SimpleNamespace(_draining=False, _cached_agent_for=lambda _route: None),
                                 profile_id="owned", instance_id="gateway",
                                 db=db, epoch=epoch)
    authority.sessions[key] = LiveSession(SimpleNamespace(platform=None, user_id="desktop"), "route")
    monkeypatch.setattr(authority, "_schedule", lambda ref: None)  # the test drives the FIFO
    try:
        from tui_gateway import owner_forward
    except ImportError:  # main: no forwarder; the submit is refused and the assertions below fail
        return authority

    endpoint = SimpleNamespace(profile_id=authority.profile_id)

    @contextlib.asynccontextmanager
    async def connect(_home, selected):
        assert selected is endpoint
        yield _OwnerClient(authority)
    monkeypatch.setattr(owner_forward, "discover_owner", lambda _home: endpoint)
    monkeypatch.setattr(owner_forward, "connect_owner", connect)
    return authority


def test_desktop_prompt_during_a_gateway_turn_runs_after_it_in_the_owner_fifo(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key, turns, emitted = _legacy(monkeypatch, db)
    try:
        db.create_session(key, "test")
        epoch = begin_runtime_epoch(db, instance_id="gateway")
        # The gateway is running a Bot Chat delivery on this session.
        bot = admit_session_input(db, epoch=epoch, principal_id="bot", session_id=key, request_id="dm-1",
                                  payload={"text": "a message_agent DM"})
        claimed = claim_session_input(db, epoch=epoch, session_id=key)
        assert claimed["admission_id"] == bot["admission_id"]
        authority = _owner(monkeypatch, db, key, epoch)

        reply = server.handle_request({"id": "p", "method": "prompt.submit",
                                       "params": {"session_id": sid, "text": "my own question"}})

        assert "error" not in reply, reply  # main: 4090 SESSION_NOT_OWNED
        assert reply["result"]["status"] == "queued" and reply["result"]["forwarded"] is True
        assert turns == [] and db.get_messages_as_conversation(key) == []  # nothing ran here
        rows = list_session_admissions(db, session_id=key)
        assert [(r["principal_id"], r["status"]) for r in rows] == [("bot", "started"), (rows[1]["principal_id"], "queued")]
        mine = rows[1]
        assert mine["admission_id"] == reply["result"]["admission_id"] and mine["payload"]["text"] == "my own question"
        # Queued, not a failed turn: the Desktop's pending-submission card, never an ``error``.
        wait_until(lambda: any(e == "session.info" for e, *_ in emitted), timeout=10)
        pending = next(p for e, _s, p in emitted if e == "session.info")["pending_submissions"]
        assert pending == [{"admission_id": mine["admission_id"], "status": "queued", "user": "my own question",
                            "input_id": mine["request_id"], "execution_generation": None, "controls": ["cancel"]}]
        assert not any(e == "error" for e, *_ in emitted)

        # The gateway turn ends; the owner's FIFO runs the prompt next, exactly once.
        assert claim_session_input(db, epoch=epoch, session_id=key) is None  # behind the running turn
        settle_session_input(db, epoch=epoch, admission_id=bot["admission_id"], generation=claimed["generation"],
                             outcome="completed")
        row = claim_session_input(db, epoch=epoch, session_id=key)
        assert row["admission_id"] == mine["admission_id"]
        assert claim_session_input(db, epoch=epoch, session_id=key) is None
        from gateway.session_contract import SessionRef
        from gateway.session_settlement_recovery import commit_and_publish
        ref, live = SessionRef("owned", key), authority.sessions[key]
        authority._stamp_claim(ref, live, row)
        with live.event_stream.lock:
            authority.executing.pop(key, None)
        commit_and_publish(authority, live, ref, row, "THE ANSWER", "completed", None, {})

        wait_until(lambda: any(e == "message.complete" for e, *_ in emitted), timeout=10)
        relayed = [(e, p) for e, s, p in emitted if s == sid and e.startswith("message.")]
        assert relayed[0][0] == "message.start"
        assert relayed[-1] == ("message.complete", {"text": "THE ANSWER", "status": "complete"})
        assert [p["pending_submissions"] for e, _s, p in emitted if e == "session.info"][-1] == []
        assert [r["status"] for r in list_session_admissions(db, session_id=key, pending_only=False)] == [
            "terminal", "terminal"]
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_outcome_unknown_work_is_still_refused_and_never_forwarded(monkeypatch, tmp_path):
    """Only Discard resolves an ``unknown`` admission: a prompt is never queued past it."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key, turns, _emitted = _legacy(monkeypatch, db)
    try:
        db.create_session(key, "test")
        first = begin_runtime_epoch(db, instance_id="gateway-1")
        lost = admit_session_input(db, epoch=first, principal_id="bot", session_id=key, request_id="dm-1",
                                   payload={"text": "lost?"})
        claimed = claim_session_input(db, epoch=first, session_id=key)
        epoch = begin_runtime_epoch(db, instance_id="gateway-2")
        recover_session_inputs(db, epoch=epoch)
        _owner(monkeypatch, db, key, epoch)
        reply = server.handle_request({"id": "p", "method": "prompt.submit",
                                       "params": {"session_id": sid, "text": "hi"}})
        assert reply["error"]["code"] == 4090
        data = reply['error']['data']
        assert data['reason'] == 'SESSION_NOT_OWNED' and data['ownership_reason'] == 'outcome_unknown'
        assert data['blocking_unknown'] == [{
            'admission_id': lost['admission_id'], 'ref': {'profile_id': 'owned', 'session_id': key},
            'status': 'unknown', 'execution_generation': claimed['generation'], 'controls': ['resolve_unknown']}]
        assert 'lost?' not in str(data)  # metadata only, not the blocked turn's private input
        receipt = server.handle_request({'id': 'refresh', 'method': 'prompt.receipt',
            'params': {'session_id': sid, 'admission_id': lost['admission_id']}})
        assert receipt['result']['status'] == 'unknown' and receipt['result']['controls'] == ['resolve_unknown']
        assert len(list_session_admissions(db, session_id=key)) == 1 and turns == []
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_a_closed_desktop_session_releases_its_relay_without_an_owner_frame(monkeypatch):
    """The relay re-checked ``_finalized`` only when an owner frame arrived, so a closed Desktop session
    held the relay thread and its owner socket until the owner's next frame (up to RELAY_TIMEOUT_S)."""
    from tui_gateway import owner_forward
    monkeypatch.setattr(owner_forward, "RELAY_FINALIZE_POLL_S", 0.05, raising=False)
    session, emitted = {}, []
    relay = owner_forward._Relay("ui", session, "key", None, None, "hi", lambda *a: emitted.append(a))

    async def scenario():
        client = SimpleNamespace(events=asyncio.Queue())  # the owner sends nothing more
        task = asyncio.create_task(relay._relay(client, "a1"))
        await asyncio.sleep(0)
        session["_finalized"] = True
        done, _ = await asyncio.wait({task}, timeout=2)
        task.cancel()
        return task in done

    assert asyncio.run(scenario()), "the relay kept its owner socket after the Desktop session closed"
    assert emitted == []  # a closed session gets no synthetic completion


def test_an_unconfirmed_forward_is_never_reported_as_not_sent(monkeypatch, tmp_path):
    """The owner's receipt timed out but the relay keeps going, so the prompt may already sit in the
    owner's FIFO: the refusal says to check the chat, with a reason the client never offers Retry for
    (a retry would admit a second copy under a fresh submission id)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key, turns, _emitted = _legacy(monkeypatch, db)
    try:
        db.create_session(key, "test")
        epoch = begin_runtime_epoch(db, instance_id="gateway")
        admit_session_input(db, epoch=epoch, principal_id="bot", session_id=key, request_id="dm-1",
                            payload={"text": "a DM"})
        claim_session_input(db, epoch=epoch, session_id=key)
        _owner(monkeypatch, db, key, epoch)
        from tui_gateway import owner_forward
        monkeypatch.setattr(owner_forward, "ADMIT_TIMEOUT_S", 0)
        hold = asyncio.Event()

        @contextlib.asynccontextmanager
        async def slow(_home, _endpoint):
            await asyncio.wait_for(hold.wait(), 5)  # the owner never answers inside the admit wait
            yield
        monkeypatch.setattr(owner_forward, "connect_owner", slow)
        reply = server.handle_request({"id": "p", "method": "prompt.submit",
                                       "params": {"session_id": sid, "text": "hi"}})
        assert reply["error"]["code"] == 4090
        assert reply["error"]["data"] == {"reason": "SESSION_OWNER_UNCONFIRMED"}
        message = reply["error"]["message"]
        assert "Nothing was sent" not in message and "check the chat" in message
        assert turns == []
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_a_receipt_lost_after_the_owner_queued_it_is_unconfirmed_not_resendable(monkeypatch, tmp_path):
    """The owner COMMITS the forwarded admission, then its socket drops before the receipt reaches
    serve (a gateway restart mid-forward). The prompt sits in the owner's durable FIFO and survives
    the restart, so "Nothing was sent. Send it again" would run a second copy: the answer is
    SESSION_OWNER_UNCONFIRMED (check the chat, no Retry). Wire-level: a real loopback websockets
    server runs the real ``AuthorityConnection``; the relay uses the real ``GatewayClient``."""
    import json
    from gateway.session_controls import AuthorityConnection
    from hermes_cli.gateway_client import GatewayClient
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
    from tui_gateway import owner_forward

    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key, turns, _emitted = _legacy(monkeypatch, db)
    try:
        db.create_session(key, "test")
        epoch = begin_runtime_epoch(db, instance_id="gateway")
        admit_session_input(db, epoch=epoch, principal_id="bot", session_id=key, request_id="dm-1",
                            payload={"text": "a DM"})
        claim_session_input(db, epoch=epoch, session_id=key)
        authority = _owner(monkeypatch, db, key, epoch)
        seen = []

        @contextlib.asynccontextmanager
        async def wire_owner(_home, _endpoint):
            loop = asyncio.get_running_loop()

            async def handler(ws):
                class _Socket:
                    def write(self, frame):
                        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(ws.send(json.dumps(frame))))
                        return True

                    def close(self):
                        return None
                conn = AuthorityConnection(authority, _Socket(), {"user_id": "desktop"})
                async for raw in ws:
                    request = json.loads(raw)
                    reply = await conn.dispatch(request)
                    seen.append(request["method"])
                    if request["method"] == "prompt.submit":
                        await ws.close()  # the gateway restarts after the admission committed
                        return
                    await ws.send(json.dumps(reply))
            async with serve(handler, "127.0.0.1", 0) as listener:
                port = listener.sockets[0].getsockname()[1]
                async with connect(f"ws://127.0.0.1:{port}", proxy=None) as ws:
                    async with GatewayClient(ws) as client:
                        yield client
        monkeypatch.setattr(owner_forward, "connect_owner", wire_owner)

        reply = server.handle_request({"id": "p", "method": "prompt.submit",
                                       "params": {"session_id": sid, "text": "my question"}})
        mine = [row for row in list_session_admissions(db, session_id=key)
                if row["payload"].get("text") == "my question"]
        assert seen == ["session.resume", "prompt.submit"] and len(mine) == 1  # the owner holds it
        recover_session_inputs(db, epoch=begin_runtime_epoch(db, instance_id="gateway-2"))
        after = {row["admission_id"]: row["status"] for row in list_session_admissions(db, session_id=key)}
        assert after[mine[0]["admission_id"]] == "queued"  # it survives the restart and will run
        assert reply["error"]["code"] == 4090
        assert reply["error"]["data"] == {"reason": "SESSION_OWNER_UNCONFIRMED"}
        message = reply["error"]["message"]
        assert "Nothing was sent" not in message and "check the chat" in message
        assert turns == []
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_an_owner_error_after_its_commit_is_unconfirmed_not_resendable(monkeypatch, tmp_path):
    """The owner publishes its pending list AFTER the durable write, and a SQLite error there reaches
    serve as a structured ``storage_unavailable``, the same answer a refused write gets. The prompt is
    in the owner's FIFO and survives a restart, so "Nothing was sent. Send it again" would queue a
    second copy under a fresh submission id: only a refusal raised before the write may say so."""
    import sqlite3
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key, turns, _emitted = _legacy(monkeypatch, db)
    try:
        db.create_session(key, "test")
        epoch = begin_runtime_epoch(db, instance_id="gateway")
        admit_session_input(db, epoch=epoch, principal_id="bot", session_id=key, request_id="dm-1",
                            payload={"text": "a DM"})
        claim_session_input(db, epoch=epoch, session_id=key)
        authority = _owner(monkeypatch, db, key, epoch)
        publish, failed = authority._publish_pending, []

        def publish_fails_once(ref):  # the admission's publication, after its commit
            if not failed:
                failed.append(ref)
                raise sqlite3.OperationalError("disk I/O error")
            return publish(ref)
        monkeypatch.setattr(authority, "_publish_pending", publish_fails_once)

        reply = server.handle_request({"id": "p", "method": "prompt.submit",
                                       "params": {"session_id": sid, "text": "my question"}})
        mine = [row for row in list_session_admissions(db, session_id=key)
                if row["payload"].get("text") == "my question"]
        assert failed and len(mine) == 1  # the owner holds it
        recover_session_inputs(db, epoch=begin_runtime_epoch(db, instance_id="gateway-2"))
        after = {row["admission_id"]: row["status"] for row in list_session_admissions(db, session_id=key)}
        assert after[mine[0]["admission_id"]] == "queued"  # it survives the restart and will run
        assert reply["error"]["code"] == 4090
        assert reply["error"]["data"] == {"reason": "SESSION_OWNER_UNCONFIRMED"}
        message = reply["error"]["message"]
        assert "Nothing was sent" not in message and "check the chat" in message
        assert turns == []
    finally:
        server._sessions.pop(sid, None)
        db.close()


@pytest.mark.parametrize("case", ["owner_refused", "owner_queue_full", "rewind"])
def test_a_prompt_the_owner_cannot_queue_is_refused_with_why_and_when(monkeypatch, tmp_path, case):
    """Where admission is impossible the refusal names why and says to send when the turn ends,
    with a transient reason (not SESSION_NOT_OWNED, whose card offers a new chat)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key, turns, _emitted = _legacy(monkeypatch, db)
    try:
        db.create_session(key, "test")
        epoch = begin_runtime_epoch(db, instance_id="gateway")
        admit_session_input(db, epoch=epoch, principal_id="bot", session_id=key, request_id="dm-1",
                            payload={"text": "a DM"})
        claim_session_input(db, epoch=epoch, session_id=key)
        params = {"session_id": sid, "text": "hi"}
        _owner(monkeypatch, db, key, epoch)
        if case == "owner_refused":
            from tui_gateway import owner_forward

            @contextlib.asynccontextmanager
            async def refused(_home, _endpoint):
                raise owner_forward.OwnerUnavailable("the gateway answered with another protocol")
                yield
            monkeypatch.setattr(owner_forward, "connect_owner", refused)
        elif case == "owner_queue_full":
            # Sent, and refused by the owner's ledger inside its write: nothing was queued.
            import hermes_state_runtime
            monkeypatch.setattr(hermes_state_runtime, "ADMISSION_QUEUE_MAX", 0)
        else:
            params.update(truncate_before_user_ordinal=0, confirm_truncate=True)
        reply = server.handle_request({"id": "p", "method": "prompt.submit", "params": params})
        assert reply["error"]["code"] == 4090 and reply["error"]["data"] == {"reason": "SESSION_OWNER_BUSY"}
        message = reply["error"]["message"]
        assert "when the current turn ends" in message and "Nothing was sent" in message
        assert {"owner_refused": "another protocol", "owner_queue_full": "the gateway refused it",
                "rewind": "rewind"}[case] in message
        assert len(list_session_admissions(db, session_id=key)) == 1 and turns == []
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_a_refused_image_send_does_not_wedge_later_text_sends(monkeypatch, tmp_path):
    """RT-37c live finding: an image send refused during a gateway turn left the image staged in the
    legacy session, so every later text-only send was refused as "a rewind or attachment" too."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key, turns, _emitted = _legacy(monkeypatch, db)
    try:
        db.create_session(key, "test")
        epoch = begin_runtime_epoch(db, instance_id="gateway")
        first = admit_session_input(db, epoch=epoch, principal_id="bot", session_id=key, request_id="dm-1",
                                    payload={"text": "a DM"})
        claimed = claim_session_input(db, epoch=epoch, session_id=key)
        _owner(monkeypatch, db, key, epoch)
        image = tmp_path / "red.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        server._sessions[sid]["attached_images"] = [str(image)]  # what image.attach stages

        refused = server.handle_request({"id": "i", "method": "prompt.submit",
                                         "params": {"session_id": sid, "text": "look at this"}})
        assert refused["error"]["data"] == {"reason": "SESSION_OWNER_BUSY"}
        assert "image" in refused["error"]["message"] and "removed" in refused["error"]["message"]  # never silent
        assert server._sessions[sid]["attached_images"] == []

        # The bot turn ends and the next one starts; the user types a plain follow-up.
        settle_session_input(db, epoch=epoch, admission_id=first["admission_id"], generation=claimed["generation"],
                             outcome="completed")
        admit_session_input(db, epoch=epoch, principal_id="bot", session_id=key, request_id="dm-2",
                            payload={"text": "another DM"})
        claim_session_input(db, epoch=epoch, session_id=key)
        reply = server.handle_request({"id": "t", "method": "prompt.submit",
                                       "params": {"session_id": sid, "text": "just text"}})

        assert "error" not in reply, reply  # before: 4090 "a rewind or attachment cannot join its queue"
        assert reply["result"]["forwarded"] is True and turns == []
    finally:
        server._sessions.pop(sid, None)
        db.close()
