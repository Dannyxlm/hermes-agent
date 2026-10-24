"""Scheduled wakes act for their exact stored chat owner, never a different login."""
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cron import bot_chat_legacy as pending, session_delivery
from gateway.session_identity import authenticated_subject
from hermes_state import SessionDB
from utils import atomic_json_write


ALICE = authenticated_subject({'provider': 'basic', 'user_id': 'alice'})
BOB = authenticated_subject({'provider': 'basic', 'user_id': 'bob'})


@pytest.fixture(params=['default', 'work'])
def wake_owner(request, tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.control_socket import GatewayControlServer
    from gateway.runtime_bootstrap import TicketStore
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from hermes_cli import gateway_client, gateway_runtime
    from hermes_constants import get_hermes_home

    user = tmp_path / 'user'
    home = tmp_path / 'state'
    home.mkdir(mode=0o700)
    (home / 'config.yaml').write_text('model: {default: fixture}\n')
    if request.param != 'default':
        home = home / 'profiles' / request.param
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    monkeypatch.setattr(Path, 'home', lambda: user)
    monkeypatch.setenv('HOME', str(user))
    monkeypatch.setenv('HERMES_HOME', str(home))
    (home / 'config.yaml').write_text('model: {default: fixture}\n')
    with SessionDB(home / 'state.db') as db:
        db.create_session('chat', source='tui', user_id='basic:alice')
        db.create_session('other', source='tui', user_id='basic:alice')
    state = SimpleNamespace(home=home, ready=False, methods=[], subjects=[],
                            forced_subject=None, drop_answer=False)
    endpoint = SimpleNamespace(profile_id=str(home), instance_id='test')
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {
        'model': {'default': 'fixture'}, 'platform_toolsets': {'cli': [], 'tui': [], 'gui': []}})
    monkeypatch.setattr(run, '_resolve_gateway_model', lambda config: 'fixture')
    monkeypatch.setattr(AuthorityConnection, '_spawn_notice', lambda *args: None)

    def discover(target, *, timeout):
        assert target == home
        return SimpleNamespace(state='ready' if state.ready else 'missing',
                               endpoint=endpoint if state.ready else None)

    @asynccontextmanager
    async def connect(*, endpoint, subject=None):
        assert get_hermes_home() == home
        state.subjects.append(subject)
        control = GatewayControlServer(home)
        control.ticket_store = TicketStore('test', frozenset({str(home)}))
        params = {'profile_id': str(home), 'instance_id': 'test', 'purpose': 'interactive'}
        forwarded = state.forced_subject or subject
        if forwarded is not None:
            params['subject'] = forwarded
        answer = json.loads(control.handle_request_line(json.dumps({
            'protocol': 1, 'id': 1, 'verb': 'session-ticket', 'params': params}).encode(),
            peer_subject='uid:1000'))
        assert answer['ok'], answer
        grant = control.ticket_store.redeem(answer['result']['ticket'], profile_id=str(home), purpose='interactive')
        store = SessionStore(home / 'sessions', GatewayConfig())
        runner = run.GatewayRunner.__new__(run.GatewayRunner)
        runner.adapters, runner.session_store, runner._session_db, runner._draining = {}, store, store._db, False
        authority = await initialize_session_authority(runner, profile_id=str(home), instance_id='test')
        authority._schedule = lambda ref: None
        conn = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None), {
            'provider': 'local', 'user_id': grant['subject'], 'profile_id': grant['profile_id'],
            'instance_id': grant['instance_id'], 'capabilities': grant['capabilities'], 'native_bootstrap': True})

        class Client:
            async def rpc(self, method, **kwargs):
                state.methods.append(method)
                result = await conn.dispatch({'id': len(state.methods), 'method': method, 'params': kwargs})
                if 'error' in result:
                    raise gateway_client.rpc_error(result['error'])
                if method == 'prompt.submit' and state.drop_answer:
                    raise ConnectionError('lost acknowledgement after admission')
                return result['result']

        try:
            yield Client()
        finally:
            await conn.close()
            authority.db.close()

    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', discover)
    monkeypatch.setattr(gateway_client, 'connect_gateway', connect)
    return state


def queue(state):
    job = {'id': 'scheduled', 'execution_id': 'run', 'name': 'fixture'}
    assert 'queued' in session_delivery.deliver_to_session(job, 'synthetic wake', 'chat')
    key = job['_bot_chat_delivery_receipts']['session:chat']['delivery_id']
    path = state.home / 'cron' / 'bot_chat_pending' / (key + '.json')
    return path, pending.read_pending(key)


def test_login_owned_tui_wake_reaches_real_admission(wake_owner):
    path, _ = queue(wake_owner)
    wake_owner.ready = True
    saved = pending._drain_one(path)
    assert saved['status'] == 'transferred', saved
    assert saved['receipt_status'] == 'queued'
    assert saved['target_session_id'] == 'chat'
    assert wake_owner.subjects == [ALICE]
    with SessionDB(wake_owner.home / 'state.db', read_only=True) as db:
        admission = db._conn.execute('SELECT principal_id,session_id FROM runtime_admissions').fetchall()
        assert [tuple(row) for row in admission] == [(ALICE, 'chat')]
        assert db.get_session('other')['session_key'] is None


def test_foreign_login_refusal_is_final_and_does_not_adopt(wake_owner):
    path, _ = queue(wake_owner)
    wake_owner.ready = True
    wake_owner.forced_subject = BOB
    saved = pending._drain_one(path)
    assert saved['status'] == 'failed', saved
    assert 'not_found' in saved['error']
    before = list(wake_owner.methods)
    pending._drain_one(path)
    pending.drain_legacy_pending()
    assert wake_owner.methods == before == ['session.resume']
    with SessionDB(wake_owner.home / 'state.db', read_only=True) as db:
        assert db.get_session('chat')['user_id'] == 'basic:alice'
        assert db.get_session('chat')['session_key'] is None
        assert db._conn.execute('SELECT count(*) FROM runtime_admissions').fetchone()[0] == 0


def test_claimed_then_lost_stays_ambiguous_and_never_replays(wake_owner):
    path, _ = queue(wake_owner)
    wake_owner.ready = True
    wake_owner.drop_answer = True
    saved = pending._drain_one(path)
    assert saved['status'] == 'ambiguous', saved
    before = list(wake_owner.methods)
    pending._drain_one(path)
    pending.drain_legacy_pending()
    assert wake_owner.methods == before == ['session.resume', 'prompt.submit']
    with SessionDB(wake_owner.home / 'state.db', read_only=True) as db:
        assert db._conn.execute('SELECT count(*) FROM runtime_admissions').fetchone()[0] == 1


def test_queued_target_cannot_be_replaced_with_another_row(wake_owner):
    path, record = queue(wake_owner)
    record['target_session_id'] = 'other'
    atomic_json_write(path, record)
    wake_owner.ready = True
    saved = pending._drain_one(path)
    assert saved['status'] == 'failed', saved
    assert 'prompt.submit' not in wake_owner.methods
    with SessionDB(wake_owner.home / 'state.db', read_only=True) as db:
        assert db.get_session('other')['session_key'] is None


def test_queued_owner_cannot_follow_a_reassigned_row(wake_owner):
    path, _ = queue(wake_owner)
    with SessionDB(wake_owner.home / 'state.db') as db:
        db._conn.execute('UPDATE sessions SET user_id=? WHERE id=?', ('basic:bob', 'chat'))
        db._conn.commit()
    wake_owner.ready = True
    saved = pending._drain_one(path)
    assert saved['status'] == 'failed', saved
    assert wake_owner.methods == []


def test_receipt_cannot_name_another_profile_home(wake_owner, tmp_path):
    path, record = queue(wake_owner)
    record['home'] = str(tmp_path / 'foreign')
    foreign = Path(record['home'])
    foreign.mkdir()
    with SessionDB(foreign / 'state.db') as db:
        db.create_session('chat', source='tui', user_id='basic:bob')
    atomic_json_write(path, record)
    wake_owner.ready = True
    saved = pending._drain_one(path)
    assert saved['status'] == 'failed', saved
    assert wake_owner.subjects == []


def test_receipt_cannot_change_its_exact_target_binding(wake_owner):
    path, record = queue(wake_owner)
    record['session_id'] = 'other'
    atomic_json_write(path, record)
    wake_owner.ready = True
    saved = pending._drain_one(path)
    assert saved['status'] == 'failed', saved
    assert wake_owner.subjects == []


@pytest.mark.parametrize('reason', ['not_found', 'permission_denied', 'profile_mismatch'])
def test_definite_pre_admission_refusal_does_not_retry(wake_owner, monkeypatch, reason):
    from hermes_cli.gateway_client import GatewayRPCError
    path, record = queue(wake_owner)

    async def refuse(*args):
        raise GatewayRPCError(reason)

    monkeypatch.setattr(session_delivery, '_wake_rpc', refuse)
    assert session_delivery.transfer_pending(path, record) is True
    assert record['status'] == 'failed'
    assert reason in record['error']
