"""Restart-safe Kanban scope placement and durable OOM attribution."""
import json
import logging
import subprocess

from hermes_cli.kanban_resources import prepare_worker_resources, resolve_worker_resources

log = logging.getLogger(__name__)


def worker_scope_argv(task, command, resources=None):
    from tools.process_registry import restart_safe_gateway_child_argv

    resources = resources or resolve_worker_resources(task)
    dispatch = restart_safe_gateway_child_argv(
        command, unit_suffix=f"kanban-{task.id}-run-{task.current_run_id or 'missing'}",
        require_restart_safe_scope=True, outlives_parent=True,
        memory_max=resources.memory_max_bytes,
    )
    if task.current_run_id is None and dispatch.mode != "in_process":
        raise RuntimeError("cannot create restart-safe systemd scope for Kanban worker: the claimed task has no current run id")
    return dispatch.argv


def spawn_with_resources(conn, spawn_fn, task, workspace, board):
    from hermes_cli.kanban_db_dispatch import _call_spawn_fn, _default_spawn

    resources = prepare_worker_resources(conn, task, board)
    if spawn_fn is None:
        return _default_spawn(task, workspace, board=board, resources=resources)
    return _call_spawn_fn(spawn_fn, task, workspace, board)


def _scope_command(unit: str, *args: str) -> str:
    from tools.process_registry import systemd_user_bus_env

    try:
        result = subprocess.run(
            ["systemctl", "--user", *args, unit], env=systemd_user_bus_env(),
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Cannot inspect Kanban scope %s: %s", unit, exc)
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def explain_worker_oom(conn, task_id: str, dead):
    """Failed scopes are retained without --collect until the run is durable.

    systemd's Result=oom-kill survives cgroup removal and gateway restarts.
    SIGKILL alone is never treated as memory exhaustion.
    """
    row = conn.execute(
        "SELECT r.metadata FROM task_runs r JOIN tasks t ON r.id=t.current_run_id WHERE t.id=?",
        (task_id,),
    ).fetchone()
    resources = json.loads(row["metadata"] or "{}").get("worker_resources") if row else None
    if not resources:
        return dead
    unit = resources["scope_unit"]
    dead.event_payload["worker_resources"] = resources
    if _scope_command(unit, "show", "--property=Result", "--value") != "oom-kill":
        return dead
    cap = resources["memory_max_bytes"]
    amount = f"{cap / 1024**3:g} GiB" if cap >= 1024**3 else f"{cap / 1024**2:g} MiB"
    name = resources["resource_class"]
    remedy = ("rerun with resource class heavy" if name == "standard" else
              "raise this class's kanban.resource_classes memory budget or reduce the workload, then rerun")
    dead.error_text = f"worker OOM-killed with the {amount} memory cap (resource class {name}); {remedy}"
    dead.event_payload.update(oom_killed=True, reason=dead.error_text)
    dead.protocol_violation = False
    dead.rate_limited = False
    return dead


def collect_worker_scopes(crash_details):
    """Only reset failed units after the run and card reason have committed."""
    for _, _, _, dead in crash_details:
        resources = dead.event_payload.get("worker_resources")
        if resources:
            _scope_command(resources["scope_unit"], "reset-failed")
