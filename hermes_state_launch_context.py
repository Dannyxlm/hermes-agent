"""Resolve a producer's write owner without bypassing managed worker receipts."""
from contextlib import contextmanager


@contextmanager
def launch_writer():
    from agent.runtime_session_store import RuntimeSessionStore, is_worker_process
    from agent.subagent_lifecycle import get_active_subagent_parent
    from hermes_state import SessionDB
    agent = get_active_subagent_parent()
    store = getattr(agent, '_session_db', None)
    if isinstance(store, RuntimeSessionStore):
        yield WorkerLaunchWriter(store)
    elif is_worker_process():
        from tools.async_delegation_worker import worker_ledger
        yield WorkerLaunchWriter(worker_ledger().store)
    elif isinstance(store, SessionDB):
        yield store
    else:
        from hermes_state_registry import acquire, release_or_close
        from hermes_constants import get_hermes_home
        db = acquire(get_hermes_home() / 'state.db')
        try:
            yield db
        finally:
            release_or_close(db)


class WorkerLaunchWriter:
    def __init__(self, store):
        self.store = store

    def _call(self, method, *args, **kwargs):
        from agent.runtime_session_store import call_off_event_loop
        return call_off_event_loop(getattr(self.store, method), *args, **kwargs)

    def record_launch(self, *args, **kwargs):
        return self._call('record_launch', *args, **kwargs)

    def bind_launch(self, *args, **kwargs):
        return self._call('bind_launch', *args, **kwargs)

    def finish_launch(self, *args, **kwargs):
        return self._call('finish_launch', *args, **kwargs)

    def schedule_launch(self, *args, **kwargs):
        return self._call('schedule_launch', *args, **kwargs)
