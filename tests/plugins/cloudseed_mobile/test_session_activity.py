"""Batch SQL work and preserve the pre-batch activity response bytes."""
import sqlite3

import pytest
from fastapi.responses import JSONResponse
from hermes_state_sessions import _LINEAGE_CTE_SQL
from plugins.cloudseed_mobile import session_activity as activity
from tests.plugins.test_cloudseed_mobile import client
from tests.plugins.test_cloudseed_mobile_activity_batch import read_batch, workers
from tui_gateway.side_task_activity import registry


@pytest.mark.parametrize('count', [1, 10, 50])
def test_batch_lineages_use_one_statement_and_preserve_outputs(client, workers, monkeypatch, count):
    c, home = client
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY,parent_session_id TEXT,end_reason TEXT)')
        db.executemany('INSERT INTO sessions VALUES(?,?,?)', [
            ('root', None, 'compression'), ('tip', 'root', None),
            ('sibling', 'root', None), ('branch', 'tip', None)])
    ids = (['tip', 'root', 'sibling', 'branch'] + [f'missing-{i}' for i in range(count)])[:count]
    owner = str(home.resolve())
    rows = [None, {'owner_profile_home': owner, 'owner_conversation_id': 'tip',
                   'subagent_id': 'a', 'status': 'running', 'started_at': 1},
            {'owner_profile_home': owner, 'owner_conversation_id': 'root',
             '_snapshot_incomplete': True},
            {'owner_profile_home': owner, 'owner_conversation_id': 'sibling',
             'subagent_id': 'b', 'status': 'queued'},
            {'owner_profile_home': owner, 'owner_conversation_id': 'branch',
             'subagent_id': 'c', 'status': 'completed'},
            {'owner_profile_home': owner + '/foreign', 'owner_conversation_id': 'tip',
             'subagent_id': 'foreign', 'status': 'running'}]
    processes = [{'owner_profile_home': owner, 'owner_conversation_id': 'root',
                  'session_id': 'p', 'status': 'running', 'started_at': 2}]
    monkeypatch.setattr(activity, '_agent_snapshot', lambda: rows)
    monkeypatch.setattr(activity, '_process_snapshot', lambda: processes)
    registry.register(home, 'root', 'fixture-task', 'background')
    # Independent pre-batch SQL and unchanged worker projection: no new batch
    # helper supplies the expected response or lineage.
    expected = []
    with sqlite3.connect(home / 'state.db') as db:
        for sid in ids:
            parents = {r[0] for r in db.execute(_LINEAGE_CTE_SQL + ' SELECT id FROM lineage', (sid, sid))}
            subagents, agents_complete = activity._scoped_workers(rows, owner, parents, 'subagent_id')
            jobs, processes_complete = activity._scoped_workers(processes, owner, parents, 'session_id')
            expected.append({'version': 1, 'profile': 'default', 'stored_session_id': sid,
                             'epoch': registry.epoch, 'complete': True,
                             'tasks': registry.snapshot(home, parents), 'subagents': subagents,
                             'subagents_complete': agents_complete, 'processes': jobs,
                             'processes_complete': processes_complete})
    body = {'version': 1, 'profile': 'default', 'epoch': registry.epoch,
            'complete': True, 'sessions': expected}
    statements = []
    real = sqlite3.connect
    def connect(*args, **kwargs):
        db = real(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db
    monkeypatch.setattr(activity.sqlite3, 'connect', connect)
    response = read_batch(c, *ids)
    assert response.content == JSONResponse(body).body
    assert len(statements) == 1
    assert expected[0]['subagents'] == [{'subagent_id': 'a', 'status': 'running', 'started_at': 1}]
    assert expected[0]['subagents_complete'] is False
    assert all(row['tasks'] == row['processes'] == row['subagents'] == []
               for row in expected if row['stored_session_id'].startswith('missing-'))
