"""Local shell snapshots belong to a qualified session, not its shared container.

Keep the existing export-p/atomic-publish wrapper: selecting an independent file
before wrapping avoids swapping mutable env state or serializing sibling commands.
Snapshot contents never enter command strings, logs, or tool results.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import os
import tempfile
import threading
from weakref import WeakKeyDictionary


_snapshot_binding: ContextVar[tuple[object, str] | None] = ContextVar("terminal_snapshot", default=None)
_snapshots = WeakKeyDictionary()
_seeds: dict[str, bytes] = {}
_lock = threading.Lock()


def _local_snapshot(env, key: str) -> str:
    """Allocate once, privately, from the spawn-time seed or login snapshot."""
    with _lock:
        paths = _snapshots.setdefault(env, {})
        if key not in paths:
            data = _seeds.get(key)
            if data is None:
                data = Path(env._snapshot_path).read_bytes()
            fd, path = tempfile.mkstemp(prefix="hermes-session-", suffix=".sh",
                                        dir=os.path.dirname(env._snapshot_path))
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
            except BaseException:
                os.unlink(path)
                raise
            paths[key] = path
        return paths[key]


@contextmanager
def session_environment(env, key: str):
    """Bind a snapshot for local terminal execution only; remote backends unchanged."""
    from tools.environments.local import LocalEnvironment

    if not isinstance(env, LocalEnvironment) or not env._snapshot_ready:
        yield
        return
    token = _snapshot_binding.set((env, _local_snapshot(env, key)))
    try:
        yield
    finally:
        _snapshot_binding.reset(token)


def command_snapshot_path(env) -> str:
    binding = _snapshot_binding.get()
    return binding[1] if binding is not None and binding[0] is env else env._snapshot_path


def capture_session_environment(task_id):
    """Read the parent's last atomic publication, without creating an environment."""
    from tools import terminal_tool as tt
    from tools.environments.local import LocalEnvironment

    with tt._env_lock:
        env = tt._active_environments.get(tt._resolve_container_task_id(task_id))
    if not isinstance(env, LocalEnvironment) or not env._snapshot_ready:
        return None
    path = _local_snapshot(env, tt._qualify_task_key(str(task_id or "default")))
    return Path(path).read_bytes()


def seed_session_environment(key: str, snapshot: bytes | None) -> None:
    if snapshot is not None:
        with _lock:
            _seeds[key] = snapshot


def clear_session_environment(key: str) -> None:
    with _lock:
        _seeds.pop(key, None)
        paths = [entries.pop(key) for entries in _snapshots.values() if key in entries]
    _remove_snapshots(paths)


def cleanup_environment_snapshots(env) -> None:
    with _lock:
        paths = list(_snapshots.pop(env, {}).values())
    _remove_snapshots(paths)


def _remove_snapshots(paths) -> None:
    for path in paths:
        for artifact in (Path(path), *Path(path).parent.glob(Path(path).name + ".tmp.*")):
            artifact.unlink(missing_ok=True)
