"""Metadata-only process evidence, without checkpoint restoration or notification consumption."""
import time
import uuid


def new_process_session(command, task_id, owner_task_id, session_key, cwd, **extra):
    from gateway.session_context import get_session_env
    from tools.activity_provenance import worker_provenance
    from tools.process_registry import ProcessSession, _is_wsl_launcher_command

    # Gateway routing keys are not SessionDB identities. Capture the scoped durable
    # id while it exists; an exact registered delegate can supply its root instead.
    stored_session_id = get_session_env("HERMES_SESSION_ID", "")
    return ProcessSession(
        id=f"proc_{uuid.uuid4().hex[:12]}", command=command, task_id=task_id,
        owner_task_id=owner_task_id, session_key=session_key, cwd=cwd,
        **worker_provenance(stored_session_id, owner_task_id),
        parent_session_id=stored_session_id,
        wsl_chain=_is_wsl_launcher_command(command), started_at=time.time(), **extra)


def process_activity_snapshot() -> list[dict]:
    from tools.process_registry import process_registry

    with process_registry._lock:
        processes = list(process_registry._running.values())
    rows = []
    for process in processes:
        with process._lock:
            if process.exited:
                continue
            # A descendant can hold stdout open after the actual command exits.
            # Observe the command handle without consuming completion notifications.
            if process.process is not None and process.process.poll() is not None:
                continue
            if process.detached and process.pid_scope == 'host':
                if process_registry._detached_host_fate(process.pid, process.host_start_time) != 'running':
                    continue
            rows.append({'session_id': process.id, 'status': 'running',
                         'started_at': process.started_at,
                         'owner_profile_home': process.owner_profile_home,
                         'owner_conversation_id': process.owner_conversation_id})
    return rows
