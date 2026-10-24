"""Delegate fan-out records its identity before execution, independent of transcripts."""
import json
from uuid import UUID

import pytest

from hermes_state import SessionDB
from tools import delegate_tool as dt


@pytest.fixture
def family_parent(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from tools import delegate_tool_config as cfg
    monkeypatch.setenv('HERMES_SINGLE_QUERY_SESSION', '1')
    monkeypatch.setattr(dt, '_load_config', dict)
    monkeypatch.setattr(cfg, '_load_config', dict)
    monkeypatch.setattr(dt, '_get_worktree_isolation', lambda: False)
    monkeypatch.setattr(dt, '_resolve_delegation_credentials', lambda *_a, **_k: {
        'model': 'fixture', 'provider': 'openrouter', 'base_url': 'https://openrouter.ai/api/v1',
        'api_key': 'fixture', 'api_mode': 'chat_completions', 'command': None, 'args': None,
    })
    events, observed = [], []
    db = SessionDB(tmp_path / 'state.db')
    parent = AIAgent(session_id='parent', session_db=db, model='fixture', provider='openrouter',
                     api_key='fixture', base_url='https://openrouter.ai/api/v1', quiet_mode=True,
                     skip_memory=True, skip_context_files=True, enabled_toolsets=['file'],
                     tool_progress_callback=lambda event, *a, **kw: events.append((event, kw)))
    parent._ensure_db_session()

    def conversation(child, **kwargs):
        child._ensure_db_session()
        with db._read_ctx() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert 'work_launches' in tables
            row = conn.execute('SELECT * FROM work_launches WHERE child_session_id=?', (child.session_id,)).fetchone()
            assert row is not None, 'launch must exist before child execution'
            observed.append(dict(row))
        return {'final_response': 'fixture complete', 'completed': True, 'api_calls': 0, 'messages': []}

    monkeypatch.setattr(AIAgent, 'run_conversation', conversation)
    yield parent, db, events, observed
    parent.close()
    db.close()


def test_fanout_titles_and_batch_survive_missing_transcripts(family_parent, monkeypatch):
    from tools import delegation_live_log
    parent, db, events, observed = family_parent
    monkeypatch.setattr(delegation_live_log, 'create_live_transcripts', lambda *a, **k: (None, [], []))
    result = json.loads(dt.delegate_task(tasks=[{'goal': 'Exercise first child', 'title': 'A' * 60},
                                               {'goal': 'Exercise second child', 'title': 'short'}], parent_agent=parent))
    spawned = [kw for event, kw in events if event == 'subagent.spawn_requested']
    assert len(spawned) == 2, result
    assert {kw.get('title') for kw in spawned} == {'A' * 39 + '…', 'short'}
    assert len(observed) == 2, result
    assert len({r['batch_id'] for r in observed}) == 1
    UUID(observed[0]['batch_id'])
    assert {r['kind'] for r in observed} == {'helper'}
    assert {r['launcher_session_id'] for r in observed} == {'parent'}
    assert {r['launcher_profile'] for r in observed} == {db.get_session('parent')['profile_name']}
    assert all(db.get_session(r['child_session_id'])['parent_session_id'] == 'parent' for r in observed)
    assert all(kw.get('batch_id') == observed[0]['batch_id'] for event, kw in events if event.startswith('subagent.'))


def test_call_level_batch_name_on_single_child(family_parent):
    parent, _db, events, observed = family_parent
    result = json.loads(dt.delegate_task(tasks=[{'goal': 'only'}], batch_name='one batch', parent_agent=parent))
    assert len(observed) == 1, result
    assert observed[0]['batch_name'] == 'one batch'
    assert observed[0]['title'] is None
    assert all(kw.get('batch_name') == 'one batch' for event, kw in events if event.startswith('subagent.'))
