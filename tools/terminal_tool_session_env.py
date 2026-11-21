"""Local shell snapshots belong to a qualified session, not its shared container.

Keep the existing export-p/atomic-publish wrapper: selecting an independent file
before wrapping avoids swapping mutable env state or serializing sibling commands.
Snapshot contents never enter command strings, logs, or tool results.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
import os
import tempfile
import threading
from weakref import WeakKeyDictionary


@dataclass(frozen=True)
class _SnapshotSeed:
    data: bytes = field(repr=False)
    generation: str
    declarations: dict[str, str] = field(repr=False)


_snapshot_binding: ContextVar[tuple[object, str] | None] = ContextVar("terminal_snapshot", default=None)
_snapshots = WeakKeyDictionary()
_seeds: dict[str, _SnapshotSeed] = {}
_lock = threading.Lock()


def _local_snapshot(env, key: str) -> str:
    """Allocate once, privately, from the spawn-time seed or login snapshot."""
    with _lock:
        paths = _snapshots.setdefault(env, {})
        if key not in paths:
            seed = _seeds.get(key)
            handoff = getattr(env, "_secret_handoff", None)
            if seed is None:
                seed = _read_seed(env._snapshot_path, handoff)
            fd, path = tempfile.mkstemp(prefix="hermes-session-", suffix=".sh",
                                        dir=os.path.dirname(env._snapshot_path))
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(seed.data)
                if handoff is not None:
                    handoff.seed(path, seed.generation, seed.declarations)
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


def snapshot_key(task_id, session_key) -> str:
    """Qualified snapshot key for a terminal call.

    A delegate_task child (a registered container alias) keys by its own task_id:
    in gateway/cron turns it inherits the parent's approval session key through
    copy_context(), so keying by that would share the parent's file again. Every
    other session keeps the session-stable key, so a gateway chat's exports survive
    the session_id rotation that context compression performs.
    """
    from tools import terminal_tool as tt

    with tt._container_alias_lock:
        is_child = bool(task_id) and task_id in tt._container_aliases
    return tt._qualify_task_key(str(task_id if is_child else (session_key or task_id or "default")))


def _read_seed(path: str, handoff) -> _SnapshotSeed:
    with open(path, "rb") as source:
        generation, declarations = handoff.capture(path, source.fileno()) if handoff is not None else ("", {})
        return _SnapshotSeed(source.read(), generation, declarations)


def capture_session_environment(task_id, session_key="") -> _SnapshotSeed | None:
    """Read the parent's last atomic publication, without creating an environment."""
    from tools import terminal_tool as tt
    from tools.environments.local import LocalEnvironment

    with tt._env_lock:
        env = tt._active_environments.get(tt._resolve_container_task_id(task_id))
    if not isinstance(env, LocalEnvironment) or not env._snapshot_ready:
        return None
    path = _local_snapshot(env, snapshot_key(task_id, session_key))
    return _read_seed(path, getattr(env, "_secret_handoff", None))


def seed_session_environment(key: str, snapshot: _SnapshotSeed | None) -> None:
    if snapshot is not None:
        with _lock:
            _seeds[key] = snapshot


def clear_session_environment(key: str) -> None:
    with _lock:
        _seeds.pop(key, None)
        paths = [(env, entries.pop(key)) for env, entries in _snapshots.items() if key in entries]
    _remove_snapshots(paths)


def cleanup_environment_snapshots(env) -> None:
    with _lock:
        paths = [(env, path) for path in _snapshots.pop(env, {}).values()]
    _remove_snapshots(paths)


def _remove_snapshots(paths) -> None:
    for env, path in paths:
        if (handoff := getattr(env, "_secret_handoff", None)) is not None:
            handoff.discard_snapshot(path)
        for artifact in (Path(path), *Path(path).parent.glob(Path(path).name + ".tmp.*")):
            artifact.unlink(missing_ok=True)
