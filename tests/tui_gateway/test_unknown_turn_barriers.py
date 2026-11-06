"""Unknown outcomes are barriers, not permission to forward or recover a turn."""
import contextlib
import os
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli.active_sessions import ActiveSessionRefusal, SESSION_NOT_OWNED
from hermes_state import SessionDB
from hermes_state_runtime import (
    admit_session_input, begin_runtime_epoch, claim_session_input, list_session_admissions,
    recover_session_inputs, register_worker_execution,
)
from tests.tui_gateway.test_auto_continue import _session
from tests.tui_gateway.test_legacy_submit_forwards_to_owner import _legacy, _owner, _OwnerClient
from tui_gateway import owner_forward, server
from tui_gateway.turn_marker import read_turn_marker, record_turn_start

pytest_plugins = ["tests.tui_gateway.test_auto_continue"]


@pytest.mark.parametrize("unknown_table", ["admission", "worker"])
def test_unknown_barrier_dominates_queued_followers(monkeypatch, tmp_path, unknown_table):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key, turns, _emitted = _legacy(monkeypatch, db)
    forwarded = []
    monkeypatch.setattr(owner_forward, "forward_prompt", lambda *a: forwarded.append(a) or {})
    try:
        db.create_session(key, "test")
        epoch = begin_runtime_epoch(db, instance_id="first")
        if unknown_table == "admission":
            admit_session_input(db, epoch=epoch, principal_id="owner", session_id=key,
                                request_id="lost", payload={"text": "may have run"})
            claim_session_input(db, epoch=epoch, session_id=key)
        else:
            register_worker_execution(db, epoch=epoch, execution_id="lost-worker", session_id=key,
                                      generation=0, kind="compute", adoption_secret="fixture-only")
        epoch = begin_runtime_epoch(db, instance_id="replacement")
        recover_session_inputs(db, epoch=epoch)
        admit_session_input(db, epoch=epoch, principal_id="owner", session_id=key,
                            request_id="follower", payload={"text": "queued behind unknown"})
        refusal = server._canonical_ledger_refusal(server._sessions[sid])
        assert refusal.ledger_status == "unknown"
        reply = server.handle_request({"id": "send", "method": "prompt.submit",
            "params": {"session_id": sid, "text": "another follower"}})
        assert reply["error"]["data"] == {"reason": SESSION_NOT_OWNED, "ownership_reason": "outcome_unknown",
                                          "blocking_unknown": []}
        assert not server._sessions[sid].get('_forwarded_admissions')
        assert forwarded == [] and turns == []
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_unavailable_ledger_owner_has_distinct_rpc_reason(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key, turns, _emitted = _legacy(monkeypatch, db)
    def unavailable(home):
        raise owner_forward.OwnerNotRunning("no ready owner")
    monkeypatch.setattr(owner_forward, "discover_owner", unavailable)
    try:
        db.create_session(key, "test")
        epoch = begin_runtime_epoch(db, instance_id="first")
        admit_session_input(db, epoch=epoch, principal_id="owner", session_id=key,
                            request_id="queued", payload={"text": "waiting"})
        reply = server.handle_request({"id": "send", "method": "prompt.submit",
            "params": {"session_id": sid, "text": "do not run here"}})
        assert reply["error"]["data"] == {"reason": SESSION_NOT_OWNED, "ownership_reason": "owner_unavailable"}
        assert turns == []
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_foreign_live_lease_has_distinct_rpc_reason(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, _key, turns, _emitted = _legacy(monkeypatch, db)
    monkeypatch.setattr(server, "_claim_active_session_slot", lambda *a, **kw:
                        (None, ActiveSessionRefusal("Session already has a live owner", SESSION_NOT_OWNED)))
    try:
        reply = server.handle_request({"id": "send", "method": "prompt.submit",
            "params": {"session_id": sid, "text": "not this writer"}})
        assert reply["error"]["data"] == {"reason": SESSION_NOT_OWNED, "ownership_reason": "owner_live_elsewhere"}
        assert turns == []
    finally:
        server._sessions.pop(sid, None)
        db.close()


@contextlib.contextmanager
def _deny_admission_bodies(db):
    """The refusal/read projection must work without selecting any admission payload."""
    original = db._read_ctx

    @contextlib.contextmanager
    def metadata_reader():
        with original() as conn:
            conn.set_authorizer(lambda action, table, column, *_: sqlite3.SQLITE_DENY if (
                action == sqlite3.SQLITE_READ and table == 'session_admissions'
                and column in {'payload_json', 'payload_digest', 'lineage_json'}) else sqlite3.SQLITE_OK)
            try:
                yield conn
            finally:
                conn.set_authorizer(None)
    db._read_ctx = metadata_reader
    try:
        yield
    finally:
        db._read_ctx = original


def _unknown_owner(monkeypatch, db, key, *, principal_id='bot'):
    db.create_session(key, 'test')
    first = begin_runtime_epoch(db, instance_id='lost-owner')
    admit_session_input(db, epoch=first, principal_id=principal_id, session_id=key,
                        request_id='private-request', payload={'text': 'private unknown body'})
    claimed = claim_session_input(db, epoch=first, session_id=key)
    epoch = begin_runtime_epoch(db, instance_id='replacement')
    recover_session_inputs(db, epoch=epoch)
    return _owner(monkeypatch, db, key, epoch), claimed


@pytest.mark.parametrize('entry', ['refusal', 'info', 'warm_resume', 'cold_resume'])
def test_unknown_identity_is_owner_verified_and_explicit_discard_never_replays(monkeypatch, tmp_path, entry):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        sid, key, turns, emitted = _legacy(monkeypatch, db)
        _authority, claimed = _unknown_owner(monkeypatch, db, key)
        seen = []
        rpc = _OwnerClient.rpc

        async def observe(client, method, **params):
            seen.append((method, params))
            return await rpc(client, method, **params)
        monkeypatch.setattr(_OwnerClient, 'rpc', observe)
        try:
            if entry == 'cold_resume':
                server._sessions.pop(sid)
                monkeypatch.setattr(server, '_enable_gateway_prompts', lambda: None)
            with _deny_admission_bodies(db):
                if entry == 'refusal':
                    reply = server.handle_request({'id': 'refused', 'method': 'prompt.submit',
                        'params': {'session_id': sid, 'text': 'never submitted'}})
                    data = reply['error']['data']
                    assert data['reason'] == SESSION_NOT_OWNED and data['ownership_reason'] == 'outcome_unknown'
                    blockers = data['blocking_unknown']
                elif entry == 'info':
                    reply = server.handle_request({'id': 'info', 'method': 'session.info', 'params': {'session_id': sid}})
                    blockers = reply['result']['blocking_unknown']
                else:
                    reply = server.handle_request({'id': 'resume', 'method': 'session.resume',
                        'params': {'session_id': key, 'source': 'desktop', 'omit_messages': True}})
                    assert 'error' not in reply, reply
                    sid = reply['result']['session_id']
                    blockers = reply['result']['info']['blocking_unknown']
            expected = {'admission_id': claimed['admission_id'], 'ref': {'profile_id': 'owned', 'session_id': key},
                        'status': 'unknown', 'execution_generation': claimed['generation'], 'controls': ['resolve_unknown']}
            assert blockers == [expected]
            assert all(method == 'prompt.receipt' for method, _ in seen), seen
            assert all(params == {'session_id': key, 'admission_id': claimed['admission_id']} for _, params in seen)
            assert len(list_session_admissions(db, session_id=key)) == 1 and turns == []
            session = server._sessions[sid]
            assert 'private unknown body' not in str(blockers) and 'private-request' not in str(blockers)
            assert session['_forwarded_admissions'][claimed['admission_id']]['observed_only'] is True
            assert owner_forward.pending_submissions(session) == []  # not this window's submitted input
            control = {'session_id': sid, 'admission_id': claimed['admission_id'],
                       'execution_generation': claimed['generation']}
            for params, reason in [({**control, 'admission_id': 'foreign'}, 'not_found'),
                                   ({**control, 'execution_generation': claimed['generation'] - 1}, 'stale_generation')]:
                result = server.handle_request({'id': 'wrong', 'method': 'prompt.resolve_unknown', 'params': params})
                assert result['error']['data']['reason'] == reason, result
            home = session.get('profile_home')
            session['profile_home'] = str(tmp_path / 'foreign-profile')
            wrong = server.handle_request({'id': 'profile', 'method': 'prompt.resolve_unknown', 'params': control})
            assert wrong['error']['data']['reason'] == 'profile_mismatch'
            session['profile_home'] = home
            session['session_key'] = 'different-scope'
            wrong = server.handle_request({'id': 'scope', 'method': 'prompt.resolve_unknown', 'params': control})
            assert wrong['error']['data']['reason'] == 'not_found'
            session['session_key'] = key
            # Ordinary Stop/Cancel must not silently discard an observed barrier.
            wrong = server.handle_request({'id': 'cancel', 'method': 'prompt.cancel', 'params': {
                'session_id': sid, 'admission_id': claimed['admission_id']}})
            assert wrong['error']['data']['reason'] == 'permission_denied'
            assert list_session_admissions(db, session_id=key)[0]['status'] == 'unknown'
            assert not any(method == 'prompt.resolve_unknown' for method, _ in seen)
            result = server.handle_request({'id': 'explicit-discard', 'method': 'prompt.resolve_unknown', 'params': control})
            assert result['result']['status'] == 'terminal' and result['result']['outcome'] == 'interrupted', result
            assert emitted[-1][2]['blocking_unknown'] == []
            assert len(list_session_admissions(db, session_id=key, pending_only=False)) == 1
            assert turns == [] and not any(method == 'prompt.submit' for method, _ in seen)
            # Only a subsequent human send starts new work; the refused body was never retained.
            reply = server.handle_request({'id': 'new-human-input', 'method': 'prompt.submit',
                'params': {'session_id': sid, 'text': 'new explicitly authorized input'}})
            assert 'error' not in reply, reply
            from tests.waiting import wait_until
            wait_until(lambda: turns == [sid], timeout=10)
            assert session['inflight_turn']['user'] == 'new explicitly authorized input'
            assert all('never submitted' not in str(value) for value in session.get('queued_prompts', []))
        finally:
            server._sessions.pop(sid, None)


@pytest.mark.parametrize('access', ['offline', 'no_control', 'other_user', 'foreign_profile', 'wrong_target', 'old_owner'])
def test_unknown_recovery_never_invents_control_or_crosses_owner_scope(monkeypatch, tmp_path, access):
    from gateway.config import Platform
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        sid, key, turns, _emitted = _legacy(monkeypatch, db)
        identity = {'user_id': 'desktop'}
        from gateway.owner_identity import principal_subject
        authority, claimed = _unknown_owner(monkeypatch, db, key, principal_id=(
            principal_subject(identity) if access == 'no_control' else 'bot'))
        if access == 'no_control':
            # Own admission can be observed without the right to discard it.
            identity['capabilities'] = ['session:read', 'session:submit']
        elif access == 'other_user':
            authority.sessions[key].source = SimpleNamespace(platform=Platform.LOCAL, user_id='someone-else')
        elif access == 'foreign_profile':
            identity['profile_id'] = 'not-owned'
        endpoint = SimpleNamespace(profile_id='owned')
        seen = []

        @contextlib.asynccontextmanager
        async def connect(home, selected):
            client = _OwnerClient(authority, identity)
            original = client.rpc

            async def rpc(method, **params):
                seen.append(method)
                result = await original(method, **params)
                if access == 'old_owner':
                    result.pop('controls', None)
                elif access == 'wrong_target':
                    result['ref']['session_id'] = 'unrelated'
                return result
            client.rpc = rpc
            yield client
        monkeypatch.setattr(owner_forward, 'discover_owner', lambda home: endpoint)
        monkeypatch.setattr(owner_forward, 'connect_owner', connect)
        if access == 'offline':
            def offline(home):
                raise owner_forward.OwnerNotRunning('offline')
            monkeypatch.setattr(owner_forward, 'discover_owner', offline)
        try:
            reply = server.handle_request({'id': 'send', 'method': 'prompt.submit',
                'params': {'session_id': sid, 'text': 'do not retain or retry'}})
            data = reply['error']['data']
            assert data['ownership_reason'] == 'outcome_unknown'
            blockers = data['blocking_unknown']
            if access in {'no_control', 'old_owner'}:
                assert blockers[0]['admission_id'] == claimed['admission_id']
                assert blockers[0]['controls'] == []
                control = server.handle_request({'id': 'discard', 'method': 'prompt.resolve_unknown', 'params': {
                    'session_id': sid, 'admission_id': claimed['admission_id'], 'execution_generation': claimed['generation']}})
                assert control['error']['data']['reason'] == 'permission_denied'
            else:
                assert blockers == []
            assert set(seen) <= {'prompt.receipt'}
            assert list_session_admissions(db, session_id=key)[0]['status'] == 'unknown'
            assert len(list_session_admissions(db, session_id=key)) == 1 and turns == []
        finally:
            server._sessions.pop(sid, None)


@pytest.mark.parametrize("writer_state", ["unknown", "alive", "dead"])
def test_foreign_recovery_requires_proven_death(monkeypatch, emits, marker_home, schedule_env, writer_state):
    record_turn_start(marker_home, "session-key", "do not replay uncertain work")
    marker = read_turn_marker(marker_home, "session-key")
    marker["writer_pid"] = os.getpid() + 1000000
    monkeypatch.setattr(server, "read_turn_marker", lambda *a: marker)
    monkeypatch.setattr(server, "marker_writer_state", lambda entry: writer_state)
    session = _session()
    result = server._maybe_schedule_auto_continue("sid", session, "session-key")
    assert (result is not None) == (writer_state == "dead")
    assert len(schedule_env) == (1 if writer_state == "dead" else 0)
    if writer_state != "dead":
        assert read_turn_marker(marker_home, "session-key") is not None
        assert not emits and not session.get("_auto_continue_scheduled")
