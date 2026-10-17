"""Kanban-only memory policy. Interactive executors deliberately have no budget."""
from dataclasses import asdict, dataclass
from pathlib import Path
import math

from hermes_platform.host.facts import ram_total_bytes

MIB = 1024 * 1024
from hermes_cli.config_defaults_kanban import DEFAULT_KANBAN_CONFIG

DEFAULT_RESOURCE_CLASSES = DEFAULT_KANBAN_CONFIG["resource_classes"]


@dataclass(frozen=True)
class WorkerResources:
    resource_class: str
    memory_max_bytes: int
    scope_unit: str

    def snapshot(self) -> dict:
        return asdict(self)


def resource_config() -> dict:
    from hermes_cli.config_effective import load_user_config_effective

    return (load_user_config_effective(fail_closed=True) or {}).get("kanban", {})


def _class_specs(config: dict) -> dict:
    overrides = config.get("resource_classes", {})
    if not isinstance(overrides, dict):
        raise ValueError("kanban.resource_classes must be a mapping")
    classes = {name: dict(spec) for name, spec in DEFAULT_RESOURCE_CLASSES.items()}
    for name, spec in overrides.items():
        if not isinstance(name, str) or not name or not isinstance(spec, dict):
            raise ValueError("Kanban resource classes must be named mappings")
        classes[name] = {**classes.get(name, {}), **spec}
    return classes


def validate_resource_class(value: str | None) -> str | None:
    """NULL inherits the board; an empty edit explicitly clears the override."""
    if value is None or value == "":
        return None
    classes = _class_specs(resource_config())
    if not isinstance(value, str) or value not in classes:
        raise ValueError(f"unknown Kanban resource class: {value!r}; configure kanban.resource_classes")
    return value


def _capacity_bytes() -> int:
    """Respect finite ancestor limits as well as physical RAM."""
    bounds = [ram_total_bytes() or 2 * 1024**3]
    try:
        relative = next(line[3:] for line in Path("/proc/self/cgroup").read_text(encoding="utf-8-sig").splitlines()
                        if line.startswith("0::"))
        root = Path("/sys/fs/cgroup")
        current = root / relative.lstrip("/")
        while current.is_relative_to(root):
            raw = (current / "memory.max").read_text(encoding="utf-8-sig").strip()
            if raw.isdigit():
                bounds.append(int(raw))
            if current == root:
                break
            current = current.parent
    except (OSError, StopIteration):
        pass
    return min(bounds)


def resolve_worker_resources(task, board: str | None = None) -> WorkerResources:
    from hermes_cli import kanban_db as kb

    config = resource_config()
    slug = board or kb.get_current_board()
    name = task.resource_class or config.get("board_resource_classes", {}).get(slug) or config.get("default_resource_class", "standard")
    classes = _class_specs(config)
    if not isinstance(name, str) or name not in classes:
        raise ValueError(f"unknown Kanban resource class: {name!r}")
    spec = classes[name]
    fraction = spec.get("ram_fraction", 0.5)
    cap = spec.get("memory_max_mb")
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not math.isfinite(fraction) or not 0 < fraction <= 0.9:
        raise ValueError(f"resource class {name}: ram_fraction must be > 0 and <= 0.9")
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 64):
        raise ValueError(f"resource class {name}: memory_max_mb must be an integer >= 64 or null")
    bound = int(_capacity_bytes() * fraction)
    if cap is not None:
        bound = min(bound, cap * MIB)
    if bound < 64 * MIB:
        raise ValueError(f"resource class {name}: available memory budget is below 64 MiB")
    return WorkerResources(name, bound, f"hermes-worker-kanban-{task.id}-run-{task.current_run_id}.scope")


def merge_run_resources(conn, run_id, metadata):
    """Completion metadata cannot erase or replace the dispatcher's budget evidence."""
    import json

    row = conn.execute("SELECT metadata FROM task_runs WHERE id=?", (run_id,)).fetchone()
    previous = json.loads(row["metadata"] or "{}") if row else {}
    if "worker_resources" not in previous:
        return metadata
    return {**(metadata or {}), "worker_resources": previous["worker_resources"]}


def prepare_worker_resources(conn, task, board: str | None) -> WorkerResources:
    """Freeze the effective budget on the run before launch, not on the mutable card."""
    import json
    from hermes_cli import kanban_db as kb

    resources = resolve_worker_resources(task, board)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET metadata = ? WHERE id = ?",
                     (json.dumps({"worker_resources": resources.snapshot()}), task.current_run_id))
    return resources
