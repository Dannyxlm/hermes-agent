"""First-turn reservation completion uses the worker-safe model-config writer."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.runtime_session_store import RuntimeSessionStore
from agent.session_source import session_source_for
from hermes_cli.profiles import get_active_profile_name
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch, mutate_worker_execution, register_worker_execution
from run_agent import AIAgent


@pytest.mark.parametrize('worker', [False, True])
def test_first_turn_completes_reserved_route_without_losing_lineage(tmp_path, worker):
    db = SessionDB(tmp_path / 'state.db')
    store = None
    try:
        db.create_session('parent', 'cli')
        db.create_session('reserved', session_source_for('cli'), model_config={'_delegate_from': 'parent'},
                          parent_session_id='parent', cwd=str(Path.cwd()), profile_name=get_active_profile_name())
        session_db = db
        if worker:
            epoch = begin_runtime_epoch(db, instance_id='fixture')
            scope = dict(epoch=epoch, execution_id='worker', session_id='reserved', generation=0)
            register_worker_execution(db, **scope, kind='compute', adoption_secret='synthetic')
            store = RuntimeSessionStore(lambda method, **params: mutate_worker_execution(db, **params),
                                        scope, tmp_path / 'outbox')
            session_db = store
        route = {'max_iterations': 12, 'max_tokens': 1024, 'reasoning_config': {'effort': 'low'}}
        agent = SimpleNamespace(
            session_id='reserved', model='fixture-model', platform='cli',
            _session_db=session_db, _session_db_created=False, _session_db_reserved=True,
            _session_row_model_config=lambda: dict(route), _cached_system_prompt='',
            _parent_session_id='parent',
        )
        AIAgent._ensure_db_session(agent)
        assert agent._session_db_created is True
        assert agent._session_db_reserved is False
        assert json.loads(db.get_session('reserved')['model_config']) == {'_delegate_from': 'parent', **route}
    finally:
        if store is not None:
            store.close()
        db.close()
