"""The canonical shared-owner /bg path creates a launch before starting its thread."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from gateway.session_controls import AuthorityConnection
from tests.gateway.test_local_source_labels import _authority


@pytest.mark.asyncio
async def test_shared_owner_background_records_bound_session_before_run(tmp_path, monkeypatch):
    from gateway import session_tui_side as side
    authority, _ = await _authority(tmp_path, monkeypatch)
    connection = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None), {'user_id': 'owner'})
    done, seen = threading.Event(), []

    def build(authority, policy, live, task_id, **kw):
        with authority.db._read_ctx() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert 'work_launches' in tables
            row = conn.execute('SELECT * FROM work_launches WHERE child_session_id=?', (task_id,)).fetchone()
            assert row is not None
            seen.append(dict(row))
        return SimpleNamespace(run_conversation=lambda **kw: {'completed': True, 'final_response': 'ok'}, close=lambda: done.set())

    monkeypatch.setattr(side, '_side_agent', build)
    try:
        created = await connection.dispatch({'id': 1, 'method': 'session.create',
                                             'params': dict(request_id='bg-parent', source='cli', cwd=str(tmp_path))})
        sid = created['result']['stored_session_id']
        spawned = await connection.dispatch({'id': 2, 'method': 'prompt.background',
                                             'params': {'session_id': sid, 'text': 'background task'}})
        assert 'result' in spawned, spawned
        assert await asyncio.to_thread(done.wait, 5), seen
        task = spawned['result']['task_id']
        assert seen[0]['launcher_session_id'] == sid
        assert seen[0]['child_session_id'] == task
        assert seen[0]['kind'] == 'background'
        assert seen[0]['title'] == 'background task'
        assert authority.db.get_session(task)['parent_session_id'] is None
    finally:
        await connection.close()
        authority.db.close()
