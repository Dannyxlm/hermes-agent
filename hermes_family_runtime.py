"""Snapshots of already-resident execution objects. Never import or recover a registry on a read."""
from pathlib import Path
import sys


def resident_signals(homes, *, can_stop_helper=lambda agent: False, can_stop_process=lambda process: False):
    by_home = {str(Path(home).resolve()): profile for profile, home in homes.items()}
    signals = {}
    module = sys.modules.get("tools.delegate_tool_registry")
    if module is not None:
        with module._active_subagents_lock:
            records = [dict(r) for r in module._active_subagents.values()]
        for record in records:
            agent = record.get("agent")
            path = getattr(getattr(agent, "_session_db", None), "db_path", None)
            sid = getattr(agent, "session_id", None)
            profile = by_home.get(str(Path(path).resolve().parent)) if path else None
            if profile and sid:
                signals[(profile, sid)] = {"running": record.get("status") in {"running", "queued", "starting"},
                                           "can_stop": can_stop_helper(agent), "backend_ref": record.get("subagent_id")}
    module = sys.modules.get("tools.process_registry")
    if module is not None:
        registry = module.process_registry
        with registry._lock:
            processes = list(registry._running.values()) + list(registry._finished.values())
            for process in processes:
                home = process.owner_profile_home
                profile = by_home.get(str(Path(home).resolve())) if home else None
                if profile is None:
                    continue
                cause = None
                if process.exited and not process._scope_stop_pending:
                    if process.completion_reason == "killed" and process.termination_source in {"process.kill", "process.stop"}:
                        cause = "user_stop"
                    elif process.completion_reason == "failed_start":
                        cause = "error"
                    elif process.completion_reason in {"exited", "already_exited"} and process.exit_code is not None:
                        cause = "success" if process.exit_code == 0 else "error"
                signals[(profile, process.id)] = {"running": not process.exited and not process.detached,
                    "terminal_cause": cause, "can_stop": can_stop_process(process), "exit_code": process.exit_code, "command": process.command[:200],
                    "backend_ref": process.id, "ended_at": process.exited_at or None, "last_activity_at": process._heartbeat_last or None}
    return signals
