"""Step kinds come from producers, not diagnostic text, with unchanged heartbeat cadence."""
from types import SimpleNamespace

import pytest

from persistence import SessionId
from persistence.conversation.sessions import NewSession
from persistence_assembly import open_persistence, profile_scope
from tests.agent.test_session_activity_persist import _agent_with_db
from tests.persistence_sqlite.core._fork_runtime import setup_store


def test_cached_turn_producer_sets_starting_without_text_classification():
    from gateway.run import GatewayRunner
    agent = SimpleNamespace()
    GatewayRunner._init_cached_agent_for_turn(agent, 0)
    assert getattr(agent, '_last_activity_kind', None) == 'starting'
    assert agent._last_activity_tool is None


def test_typed_steps_share_existing_heartbeat_window_and_clear_to_idle(tmp_path, monkeypatch):
    agent = _agent_with_db()
    db = open_persistence(profile_scope(home=tmp_path))
    sid = SessionId('sess-1')
    db.sessions.create(NewSession(session_id=sid, source='cli'))
    agent.persistence = db

    def activity():
        row = db.sessions.get(sid)
        assert row is not None
        return row

    from agent import activity_tracking
    mono, wall = [1000.0], [1700000000.0]
    monkeypatch.setattr(activity_tracking.time, 'monotonic', lambda: mono[0])
    monkeypatch.setattr(activity_tracking.time, 'time', lambda: wall[0])
    try:
        agent._touch_activity('arbitrary identical text', step_kind='tool', step_tool='terminal')
        row = activity()
        assert (row.last_activity_kind, row.last_activity_tool) == ('tool', 'terminal')
        mono[0] += 1
        wall[0] += 1
        agent._touch_activity('arbitrary identical text', step_kind='writing')
        assert agent.get_activity_summary()['step_kind'] == 'writing'
        assert activity().last_activity_kind == 'tool'
        mono[0] += 60
        wall[0] += 60
        agent._touch_activity('arbitrary identical text', step_kind='thinking')
        assert activity().last_activity_kind == 'thinking'
        agent._reset_activity_labels_after_turn()
        row = activity()
        assert row.last_activity_kind == 'idle'
        assert row.last_activity_tool is None
        assert row.last_activity_at == wall[0]
    finally:
        db.close()


def test_worker_activity_has_closed_step_fields_and_preserves_generation_fence(tmp_path):
    from persistence.core.errors import AuthorityRefusal as RuntimeStoreError
    from persistence.sqlite.workers._receipt import mutate_worker_execution
    db, store, scope = setup_store(tmp_path)
    try:
        store.touch_session_activity('owned', description='not parsed', step_kind='waiting_helpers')
        assert db.get_session('owned')['last_activity_kind'] == 'waiting_helpers'
        with pytest.raises(RuntimeStoreError, match='invalid_params'):
            mutate_worker_execution(db, **scope, sequence=store.journal['next_sequence'], operation='session.activity',
                                    payload={'ts': None, 'description': 'no', 'provenance': None,
                                             'step_kind': 'invented', 'step_tool': None})
        store.clear_session_activity_labels('owned')
        assert db.get_session('owned')['last_activity_kind'] == 'idle'
    finally:
        store.close()
        db.close()
