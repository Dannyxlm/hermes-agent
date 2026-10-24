"""Tracked local and sandbox launches, separate from registry lifecycle and output."""
from __future__ import annotations
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tools.process_registry import ProcessSession


class ProcessLaunchMixin:
    def spawn_local(
        self, command: str, cwd: str | None = None, task_id: str = "", session_key: str = "",
        env_vars: dict | None = None, use_pty: bool = False, owner_task_id: str = "",
        persist_on_release: bool = False) -> ProcessSession:
        """Spawn a background process locally (TERMINAL_ENV=local; other backends use
        spawn_via_env()). ``use_pty`` requests a pseudo-terminal via ptyprocess/pywinpty
        for interactive CLIs, falling back to a plain pipe when unavailable or failing.
        ``persist_on_release`` keeps the process out of agent-lifecycle kill sweeps (#41225)."""
        from tools.process_registry import os, _resolve_safe_cwd
        # Bash parses ``A && B &`` as ``(A && B) &`` — a subshell that holds our stdout
        # pipe open forever when B is a long-running server. The rewriter turns it into
        # ``A && { B & }``. Lazy import: terminal_tool imports this module.
        # Guard against the `A && B &` subshell-wait trap (issue #68915).
        from tools.terminal_tool_sudo import _rewrite_compound_background as _rewrite_bg

        safe_command = _rewrite_bg(command)
        session = self._new_session(command, task_id, owner_task_id, session_key, _resolve_safe_cwd(cwd or os.getcwd()),
                                    persist_on_release=persist_on_release)
        try:
            result = self._spawn_local_reserved(session, safe_command, env_vars, use_pty)
        except BaseException:
            from tools.process_launches import finish_process_launch, record_process_launch
            record_process_launch(session)
            finish_process_launch(session, "failed_start")
            raise
        from tools.process_launches import record_process_launch
        record_process_launch(session)
        return result

    def _spawn_local_reserved(self, session, safe_command, env_vars, use_pty):
        from tools.process_registry import (
            worker_output_log, record_exit_command, logger, windows_hide_flags,
            _IS_WINDOWS, subprocess, systemd_user_bus_env, open_output_log, os, _stop_systemd_unit)
        pty_scope_attempted = False
        # A worker's pipe or PTY dies with the worker (the child's next write is SIGPIPE): write to a log.
        log = worker_output_log(session.id)
        if use_pty and log is None:
            try:
                return self._spawn_local_pty(session, safe_command, env_vars)
            except ImportError:
                logger.warning("ptyprocess not installed, falling back to pipe mode")
            except Exception as e:
                logger.warning("PTY spawn failed (%s), falling back to pipe mode", e)
                if session.systemd_unit:
                    pty_scope_attempted = True
                    if not _stop_systemd_unit(session.systemd_unit):
                        raise RuntimeError(
                            "PTY scope could not be reaped; refusing pipe fallback "
                            "to avoid duplicate command execution"
                        ) from e
                    session.systemd_unit = ""
        # Pipe path (non-PTY or PTY fallback).
        _popen_kwargs = {"creationflags": windows_hide_flags()} if _IS_WINDOWS else {}
        unit_suffix = f"{session.id}-pipe-fallback" if pty_scope_attempted else session.id
        spawn_argv = self._scope_argv(session, record_exit_command(log, safe_command) if log else safe_command,
                                      unit_suffix, "Local")
        spawn_env = self._spawn_env(env_vars)
        if session.systemd_unit:
            spawn_env = systemd_user_bus_env(spawn_env)
        # start_new_session is REQUIRED with systemd-run --scope too: the scope does not
        # give the worker a new session, so from an interactive TUI the worker would
        # share the foreground process group and background spawns would stop the whole
        # session (observed as dead TUIs in state T). Cgroup isolation is unaffected —
        # the scope attaches to the invoked process, not the spawning session.
        output = open_output_log(log) if log else subprocess.PIPE
        try:
            proc = subprocess.Popen(
                spawn_argv, text=True, cwd=session.cwd, env=spawn_env, encoding="utf-8",
                errors="replace", stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True, **_popen_kwargs)
        finally:
            if log:
                os.close(output)
        session.process, session.output_log = proc, str(log or "")
        session.pid = proc.pid
        session.host_start_time = self._safe_host_start_time(session.pid)
        try:
            self._track_started(session, self._log_reader_loop if log else self._reader_loop, f"proc-reader-{session.id}")
        except Exception:
            self._reap_untracked(session, proc)
            raise
        return session

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
