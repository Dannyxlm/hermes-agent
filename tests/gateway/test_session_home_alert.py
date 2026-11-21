"""A producer turn's Bot Chat reply alerts the phone only when it opens with [NEEDS YOU].

A real authority, ``SessionDB`` and ``PushService`` outbox drive the admission drain; only the model
(the turn's reply) and Apple (the sender) are stand-ins.
"""
import uuid
from types import SimpleNamespace

import pytest

from gateway import session_home_alert
from gateway.session_contract import SessionActor, Submission
from tui_gateway.mobile_push import PushService, Scope
from tui_gateway.mobile_push_provider import DeliveryResult


class Sender:
    def __init__(self):
        self.jobs = []

    def send(self, job):
        self.jobs.append(job)
        return DeliveryResult("accepted")

    def close(self):
        pass


ACTOR = SessionActor('test-owner', 'default', frozenset({'session:create', 'session:submit', 'session:read'}), 'fixture')


async def _home(tmp_path, monkeypatch, *, categories=("attention",), title='Bot Chat'):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import SessionAuthority, initialize_session_authority
    from gateway.session_local import create_local_session
    from gateway.session_local_title import title_new_session
    from hermes_state import SessionDB
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)  # the test drains
    monkeypatch.setattr('gateway.session_bot._watch_reply', lambda *args: None)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    db = SessionDB(tmp_path / 'state.db')
    runner.session_store._db = db
    runner._session_db = db
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='needs-you', db=db)
    ref = create_local_session(authority, ACTOR, dict(request_id='bot', source='gui', model='fixture', toolsets=[]))
    title_new_session(authority, ref, title)
    authority.sessions[ref.session_id].subscribers['viewer'] = ACTOR  # Home is open somewhere
    sender = Sender()
    service = PushService(tmp_path / 'outbox.sqlite3', sender)
    monkeypatch.setattr(session_home_alert, 'push_service', lambda: service)
    scope = Scope('native', 'default', ref.session_id)
    service.register('principal', scope, installation_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()),
                     token='aa' * 32, environment='production', categories=list(categories), preview_enabled=True)
    return SimpleNamespace(runner=runner, authority=authority, ref=ref, service=service, sender=sender, db=db)


def _reply_with(home, text):
    async def handle(event):
        return text
    home.runner._handle_message = handle


async def _producer_turn(home, text):
    """A scheduled report delivered with deliver=bot-chat: a producer admission, then the drain."""
    from gateway.session_bot import deliver
    connection = SimpleNamespace(authority=home.authority, actor=ACTOR)
    await deliver(connection, dict(id=uuid.uuid4().hex, profile='default', message='[Cronjob "Box doctor"] report'))
    _reply_with(home, text)
    await home.authority._drain(home.ref)
    home.service.drain_once()
    return home.sender.jobs


@pytest.mark.asyncio
async def test_a_producer_reply_that_needs_you_alerts_as_attention_once(tmp_path, monkeypatch):
    home = await _home(tmp_path, monkeypatch)  # only "Someone needs you" is on
    try:
        jobs = await _producer_turn(home, '[NEEDS YOU] The lease renewal needs your signature by Friday.')
        assert len(jobs) == 1
        payload = jobs[0]['payload']
        assert payload['hermex.status'] == 'complete'
        assert payload['hermex.destination']['surface'] == 'native'
        assert payload['hermex.destination']['canonical_root_id'] == home.ref.session_id
        # The marker is the producer's signal, never the alert's words; the agent titles it, not "default".
        assert payload['aps']['alert']['body'] == 'The lease renewal needs your signature by Friday.'
        assert payload['aps']['alert']['title'] == 'Ava'
    finally:
        home.service.close()
        home.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    "Box doctor's report checks out. Nothing here needs you tonight.",
    "Routine summary.\n[NEEDS YOU] quoted later is prose, not the marker.",
])
async def test_routine_producer_replies_only_stay_unread(tmp_path, monkeypatch, text):
    home = await _home(tmp_path, monkeypatch, categories=('attention', 'completion'))
    try:
        assert await _producer_turn(home, text) == []
    finally:
        home.service.close()
        home.db.close()


@pytest.mark.asyncio
async def test_a_viewer_turn_never_takes_the_producer_path(tmp_path, monkeypatch):
    """Danny's own prompt already alerts through its turn's push projection: no second alert here."""
    home = await _home(tmp_path, monkeypatch, categories=('attention', 'completion'))
    try:
        await home.authority.submit(ACTOR, Submission('r1', home.ref, {'text': 'what is left?'}, 'queue'))
        _reply_with(home, '[NEEDS YOU] Pick a date.')
        await home.authority._drain(home.ref)
        home.service.drain_once()
        assert home.sender.jobs == []
    finally:
        home.service.close()
        home.db.close()


def test_only_the_canonical_chats_current_tip_is_home(tmp_path):
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('root', source='tui')
        db.set_session_title('root', 'Bot Chat')
        db.create_session('other', source='tui')
        assert session_home_alert._canonical_root(db, 'root') == 'root'
        assert session_home_alert._canonical_root(db, 'other') is None, 'another chat is not Home'
        db._conn.execute("UPDATE sessions SET archived=1 WHERE id='root'")
        db._conn.commit()
        assert session_home_alert._canonical_root(db, 'root') is None, 'an archived Home alerts no one'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_broken_outbox_never_fails_the_turn(tmp_path, monkeypatch):
    from hermes_state_runtime import list_session_admissions
    home = await _home(tmp_path, monkeypatch)
    try:
        def broken():
            raise OSError('disk gone')
        monkeypatch.setattr(session_home_alert, 'push_service', broken)
        assert await _producer_turn(home, '[NEEDS YOU] still delivered') == []
        rows = list_session_admissions(home.db, session_id=home.ref.session_id, pending_only=False)
        assert [row['outcome'] for row in rows] == ['completed']
    finally:
        home.service.close()
        home.db.close()


def test_marker_is_read_case_insensitively_after_leading_whitespace():
    assert session_home_alert.needs_you_body('  [needs you]  Pick a date.') == 'Pick a date.'
    assert session_home_alert.needs_you_body('[NEEDS YOU]') == ''
    assert session_home_alert.needs_you_body('[NEEDS] YOU') is None
    assert session_home_alert.needs_you_body(None) is None


def test_an_explicit_category_overrides_the_status_and_is_validated(tmp_path):
    sender = Sender()
    service = PushService(tmp_path / 'outbox.sqlite3', sender)
    try:
        scope = Scope('native', 'default', 'root')
        service.register('principal', scope, installation_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()),
                         token='bb' * 32, environment='production', categories=['completion'], preview_enabled=False)
        run = service.start_run(scope)
        # Attention-only alerts never reach a device that asked only for finished replies.
        assert service.record(scope, run['run_id'], 'e1', 'complete', category='attention')
        service.drain_once()
        assert sender.jobs == []
        with pytest.raises(ValueError):
            service.record(scope, service.start_run(scope)['run_id'], 'e2', 'complete', category='loud')
    finally:
        service.close()


def test_bot_mode_protocol_version_bumped_for_needs_you_rule():
    from tools import bot_mode_probe
    assert bot_mode_probe._BOT_MODE_PROTOCOL_VERSION >= 4


@pytest.mark.asyncio
async def test_all_chats_policy_receives_needs_you_without_explicit_home_lease(tmp_path, monkeypatch):
    """A device on the 'all' alert policy materialises its Home lease via listed=True."""
    home = await _home(tmp_path, monkeypatch)
    try:
        # Replace the explicit Home lease with an installation-level 'all' alert policy.
        home.service._store._conn.execute("DELETE FROM alert_subscriptions")
        home.service._store._conn.commit()
        home.service.set_alert_policy(
            'principal',
            installation_id=str(uuid.uuid4()),
            connection_id=str(uuid.uuid4()),
            token='cc' * 32,
            environment='production',
            mode='all',
            categories=['attention'],
            preview_enabled=True,
        )
        jobs = await _producer_turn(home, '[NEEDS YOU] Urgent approval waiting.')
        assert len(jobs) == 1
        assert jobs[0]['payload']['aps']['alert']['body'] == 'Urgent approval waiting.'
    finally:
        home.service.close()
        home.db.close()
