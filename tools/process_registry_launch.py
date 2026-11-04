"""Tracked local and sandbox launches, separate from registry lifecycle and output."""
from __future__ import annotations
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tools.process_registry import ProcessSession


class ProcessLaunchMixin:
    @staticmethod
    def _spawn_env(env_vars: dict) -> dict:
        """Sanitized terminal child env with task-local attribution, also used by PTY launches."""
        from agent.session_client_env import toolchain_environ
        from tools.process_registry import _terminal_env, _sanitize_subprocess_env
        from tools.environments.local import _stamp_terminal_launcher
        env = _terminal_env(_sanitize_subprocess_env(toolchain_environ(), env_vars))
        _stamp_terminal_launcher(env)
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def spawn_via_env(
        self, env: Any, command: str, cwd: str | None = None, task_id: str = "", session_key: str = "",
        timeout: int = 10, owner_task_id: str = "", persist_on_release: bool = False) -> ProcessSession:
        """Spawn a background process inside a non-local backend's sandbox.
        The command is wrapped to capture its in-sandbox PID and redirect output to a
        log file that later execute() calls poll. No live pipe or stdin, but it runs in
        the correct sandbox context. ``persist_on_release`` keeps the process out of
        agent-lifecycle kill sweeps (#41225)."""
        session = self._new_session(command, task_id, owner_task_id, session_key, cwd, env_ref=env, pid_scope="sandbox",
                                    persist_on_release=persist_on_release)
        try:
            result = self._spawn_env_reserved(session, env, command, cwd, timeout)
        except BaseException:
            from tools.process_launches import finish_process_launch, record_process_launch
            record_process_launch(session)
            finish_process_launch(session, "failed_start")
            raise
        from tools.process_launches import record_process_launch
        record_process_launch(session)
        return result

    def _spawn_env_reserved(self, session, env, command, cwd, timeout):
        import shlex
        temp_dir = self._env_temp_dir(env)
        log_path, pid_path, exit_path = (f"{temp_dir}/hermes_bg_{session.id}.{ext}" for ext in ("log", "pid", "exit"))
        q = shlex.quote
        bg_command = (
            f"mkdir -p {q(temp_dir)} && "
            f"( nohup bash -lc {q(command)} > {q(log_path)} 2>&1; "
            f"rc=$?; printf '%s\\n' \"$rc\" > {q(exit_path)} ) & "
            f"echo $! > {q(pid_path)} && cat {q(pid_path)}")
        result = env.execute(bg_command, cwd=cwd or "", timeout=timeout, rewrite_compound_background=False)
        output = result.get("output", "").strip()
        session.pid = next((int(ln) for ln in map(str.strip, output.splitlines()) if ln.isdigit()), None)
        # No PID from the wrapper (no bash in the image, sandbox gone, broken redirect): the
        # command never started, so raise like a failed Popen instead of returning a session.
        if session.pid is None:
            raise RuntimeError(f"launch wrapper exited {result.get('returncode')} without a PID: "
                               f"{output or '(no output)'}")
        self._track_started(
            session, self._env_poller_loop, f"proc-poller-{session.id}", (env, log_path, pid_path, exit_path))
        return session
