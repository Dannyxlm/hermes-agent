"""Result-shape adapters for the fork regression cases moved with SQLite."""
from persistence.core.errors import AuthorityRefusal as RuntimeStoreError
from persistence.sqlite.authority import _admission_store, _mutation
from persistence.sqlite.authority._runtime_ends import begin_runtime_epoch
from persistence.sqlite.workers._executions import register_worker_execution
from persistence.sqlite.workers._receipt import mutate_worker_execution

claim_session_input = _admission_store.claim_session_input
get_session_admission = _admission_store.get_session_admission
settle_session_input = _admission_store.settle_session_input
recover_session_inputs = _admission_store.recover_session_inputs


def admit_session_input(holder, **kwargs):
    return _admission_store.admit_session_input(holder, **kwargs)[0]


def mutate_runtime_session(holder, **kwargs):
    return _mutation.mutate_runtime_session(holder, **kwargs)[0]


def setup_store(tmp_path):
    from agent.runtime_session_store import RuntimeSessionStore
    from tests.persistence_sqlite.core._fork_store import SessionDB

    db = SessionDB(tmp_path / 'state.db')
    db.create_session('owned', 'cli', cwd='/reserved', profile_name='fixture')
    db.create_session('foreign', 'cli', system_prompt='synthetic')
    epoch = begin_runtime_epoch(db, instance_id='fixture')
    scope = dict(epoch=epoch, execution_id='worker', session_id='owned', generation=0)
    register_worker_execution(db, **scope, kind='compute', adoption_secret='synthetic-proof')
    store = RuntimeSessionStore(lambda method, **kwargs: mutate_worker_execution(db, **kwargs), scope,
                                tmp_path / 'outbox')
    return db, store, scope


def reserve_child(db, epoch, sequence):
    import os
    import psutil
    from hermes_cli.process_identity import worker_birth
    from persistence.sqlite.workers._gateway import reserve_child as reserve

    return reserve(db, epoch=epoch, execution_id='worker', session_id='parent', generation=0,
                   sequence=sequence, payload={'secret': f'child-secret-{sequence:08d}',
                       'launch': {'launch_id': f'child:{sequence}', 'batch_id': 'test', 'batch_name': None, 'title': None}},
                   identity={'profile_id': 'home', 'pid': os.getpid(), 'birth': worker_birth(psutil.Process())},
                   principal='local', limits=(2, 1), refused=None)
