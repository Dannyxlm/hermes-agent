"""Accident guard: every signal target is a process spawned by this test tree."""

import json
import os
import selectors
import signal
import subprocess
import sys
from contextlib import contextmanager

import pytest


# The stand-in owns both the guarded shell and the targets. Never signal a discovered parent.
_HOST = r'''
import json, logging, os, signal, sys
logging.basicConfig(level=logging.INFO)
logging.getLogger("tools.environments.host_stop_guard").setLevel(logging.DEBUG)
from tools.environments.local import LocalEnvironment
import tools.process_registry as registry_module
registry_module._SYSTEMD_SCOPE_AVAILABLE = False

def stop(sig, frame):
    print(json.dumps({"stopped": sig}), flush=True)
    raise SystemExit(0)

for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, stop)
print(json.dumps({"ready": os.getpid()}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    command = request["command"]
    if request.get("background"):
        registry = registry_module.ProcessRegistry()
        session = registry.spawn_local(command, cwd=os.getcwd())
        session.process.wait(timeout=10)
        # Reader completion is independent of process exit.
        from tests.waiting import wait_until
        wait_until(lambda: session.exited, timeout=10)
        result = {"output": session.output_buffer, "returncode": session.exit_code}
    else:
        env = LocalEnvironment(cwd=os.getcwd(), persistent=False)
        try:
            result = env.execute(command, timeout=10)
        finally:
            env.cleanup()
    print(json.dumps(result), flush=True)
'''


def _read_reply(proc):
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=30), "owned stand-in did not reply"
    line = proc.stdout.readline()
    assert line, "stray kill terminated the owned stand-in instead of being refused"
    return json.loads(line)


@contextmanager
def _host(tmp_path):
    child_env = {key: os.environ[key] for key in ("PATH", "HOME", "HERMES_HOME", "TMPDIR") if key in os.environ}
    child_env["PYTHONPATH"] = os.getcwd()
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", _HOST], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=child_env, start_new_session=True,
    )
    try:
        assert _read_reply(proc) == {"ready": proc.pid}
        yield proc
    finally:
        if proc.poll() is None:
            proc.stdin.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()  # Only the Popen-returned, still-owned process.
                proc.wait(timeout=10)
        proc.stdout.close()
        proc.stderr.close()


def _execute(proc, command, *, background=False):
    proc.stdin.write(json.dumps({"command": command, "background": background}) + "\n")
    proc.stdin.flush()
    return _read_reply(proc)


@pytest.mark.platforms("linux", "macos")
@pytest.mark.parametrize("spelling", ["", "-TERM ", "-INT ", "-HUP ", "-9 ", "-s TERM ", "-n 15 "])
def test_expanded_kill_cannot_stop_own_host(tmp_path, spelling):
    with _host(tmp_path) as host:
        result = _execute(host, f"target={host.pid}; kill {spelling}\"$target\"")
        if "stopped" in result:
            host.wait(timeout=10)
            pytest.fail(f"owned stand-in stopped: {result}; stderr: {host.stderr.read()}")
        assert result["returncode"] != 0
        assert result["output"].count("host-stop guard: refused") == 1
        assert str(host.pid) in result["output"]
        assert "signal=" in result["output"]
        assert host.poll() is None


@pytest.mark.platforms("linux", "macos")
@pytest.mark.parametrize("background", [False, True])
def test_host_process_group_is_refused(tmp_path, background):
    with _host(tmp_path) as host:
        # start_new_session makes this owned host's PGID equal its Popen PID.
        result = _execute(host, f"kill -- -{host.pid}", background=background)
        assert result["returncode"] != 0
        assert "host-stop guard: refused" in result["output"]
        assert host.poll() is None


@pytest.mark.platforms("linux", "macos")
def test_normal_commands_probes_and_owned_child_cleanup(tmp_path):
    with _host(tmp_path) as host:
        result = _execute(host, f"kill -0 {host.pid}; kill -l TERM; printf 'unchanged\\n'")
        assert result["returncode"] == 0
        assert "unchanged" in result["output"]
        assert "host-stop guard" not in result["output"]
        # Each $! is exactly the PID returned by this shell's spawn, not a process lookup.
        result = _execute(host, "sleep 60 & child=$!; kill \"$child\"; wait \"$child\"; printf 'child-reaped\\n'")
        assert "child-reaped" in result["output"]
        assert "host-stop guard" not in result["output"]
        result = _execute(host, "sleep 60 & kill $!; wait $!; printf 'bang-reaped\\n'")
        assert "bang-reaped" in result["output"]
        assert "host-stop guard" not in result["output"]


@pytest.mark.platforms("linux", "macos")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", signal.SIGTERM)])
def test_external_stop_still_runs_existing_callback(tmp_path, sig):
    with _host(tmp_path) as host:
        assert _execute(host, "printf ready")["returncode"] == 0
        host.send_signal(sig)  # The process returned by this test's Popen only.
        assert _read_reply(host) == {"stopped": sig}
        assert host.wait(timeout=10) == 0


@pytest.mark.platforms("linux", "macos")
@pytest.mark.parametrize("error", [OSError, TimeoutError, ImportError])
def test_discovery_errors_fail_open(monkeypatch, error):
    from tools.environments import host_stop_guard

    def unavailable():
        raise error("process inspection unavailable")

    monkeypatch.setattr(host_stop_guard, "_protected_targets", unavailable)
    command = "kill 99999999"
    assert host_stop_guard.guard_shell_command(command, shell="/bin/bash") == command


def test_no_kill_command_never_inspects_processes(monkeypatch):
    from tools.environments import host_stop_guard

    def unexpected():
        pytest.fail("ordinary commands must not pay for process inspection")

    monkeypatch.setattr(host_stop_guard, "_protected_targets", unexpected)
    command = "printf 'unchanged\\n'"
    assert host_stop_guard.guard_shell_command(command, shell="/bin/bash") == command


@pytest.mark.platforms("linux", "macos")
def test_mixed_targets_refused_before_signaling_anything(tmp_path):
    with _host(tmp_path) as host:
        result = _execute(host, f"sleep 60 & child=$!; kill \"$child\" {host.pid}; rc=$?; "
                          "kill -0 \"$child\" && printf 'child-alive\\n'; "
                          "kill \"$child\"; wait \"$child\"; exit \"$rc\"")
        assert result["returncode"] == 1
        assert result["output"].count("host-stop guard: refused") == 1
        assert "child-alive" in result["output"]
        assert host.poll() is None


@pytest.mark.platforms("linux", "macos")
def test_explicit_binary_bypass_is_fail_open(tmp_path):
    with _host(tmp_path) as host:
        result = _execute(host, f"/bin/kill -TERM {host.pid}")
        assert result == {"stopped": signal.SIGTERM}
        assert host.wait(timeout=10) == 0


@pytest.mark.platforms("linux", "macos")
def test_unsupported_shell_is_untouched():
    from tools.environments.host_stop_guard import guard_shell_command

    command = "printf unchanged"
    assert guard_shell_command(command, shell="/bin/zsh") == command
