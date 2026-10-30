"""Real finite CLI paths inherit attribution from the terminal, not test-injected provenance."""
import asyncio
from contextlib import closing
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from hermes_state import SessionDB
from tests.gateway.fixtures.local_recovery_probe import Model, child_env, daemon, rpc, websocket
from tools.environments.local import _make_run_env

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("shape", ["query_file", "quiet", "unattended", "thin_error", "teammate", "nested", "thin_stop"])
def test_terminal_finite_cli_launch(tmp_path, monkeypatch, shape):
    class Provider(Model):
        def do_POST(self):
            if shape == "thin_error":
                self.send_error(400, "Deliberate inference rejection")
            else:
                super().do_POST()

    peer = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    peer.requests = []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    user = tmp_path / "user"
    user.mkdir()
    home = tmp_path / "state"
    target = home / "profiles" / "atlas" if shape == "teammate" else home
    target.mkdir(parents=True)
    url = f"http://127.0.0.1:{peer.server_port}/v1"
    config = {"gateway": {"multiplex_profiles": False},
              "model": {"provider": "custom", "base_url": url, "default": "test-model", "api_mode": "chat_completions"},
              "auxiliary": {"title_generation": {"enabled": False}},
              "memory": {"memory_enabled": False, "user_profile_enabled": False},
              "platform_toolsets": {"cli": []}}
    (target / "config.yaml").write_text(json.dumps(config))
    with SessionDB(home / "state.db") as db:
        db.create_session("family-parent", "cli", profile_name="default")
        if shape == "nested":
            db.record_launch("unit-parent", kind="process", launcher_profile="default",
                             launcher_session_id="family-parent", child_profile="default", backend_ref="systemd:example")
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(target),
               OPENAI_API_KEY="loopback-only", OPENAI_BASE_URL=url, PYTHONPATH=str(ROOT),
               HERMES_MANAGED_DIR=str(tmp_path / "managed"), TERMINAL_CWD=str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    tokens = set_session_vars(session_id="family-parent", profile="default")
    try:
        terminal_env = _make_run_env({})
    finally:
        clear_session_vars(tokens)
    # Keep the isolated daemon's explicit env; attribution is entirely from the real builder.
    env.update({k: v for k, v in terminal_env.items() if k.startswith("HERMES_STARTED_BY_")})
    if shape == "nested":
        env["HERMES_LAUNCH_PARENT_ID"] = "unit-parent"
    args = ["chat", "-q", "hello", "-Q"]
    if shape == "query_file":
        query = tmp_path / "query.txt"
        query.write_text("hello")
        args = ["chat", "--query-file", str(query), "--oneshot", "-Q"]
    elif shape in {"unattended", "thin_error", "thin_stop"}:
        args = ["-z", "BLOCK_STARTED" if shape == "thin_stop" else "hello"]
    elif shape == "teammate":
        args = ["-p", "atlas", *args]
    # Explicit profile selection must resolve under the caller's HOME.
    cli_env = {**env, "HERMES_HOME": str(home)}
    daemon_env = dict(env)
    for key in list(daemon_env):
        if key.startswith("HERMES_STARTED_BY_"):
            daemon_env.pop(key)
    try:
        with daemon(ROOT, target, daemon_env, barrier=False) as (_, desc):
            command = [sys.executable, "-c", "from hermes_cli.main import main; main()", *args]
            result = _run_cli(command, cli_env, target, peer, desc, stop=shape == "thin_stop")
            assert result.returncode == {"thin_error": 2, "thin_stop": 130}.get(shape, 0), (result.stdout, result.stderr)
            with closing(sqlite3.connect(f"file:{target / 'state.db'}?mode=ro", uri=True)) as db:
                db.row_factory = sqlite3.Row
                launches = [dict(r) for r in db.execute("SELECT * FROM work_launches")]
            if shape == "nested":
                from hermes_family import Family, read_profile
                family = Family({"default": read_profile("default", home / "state.db")}).session_family("default", "family-parent")
                by_kind = {n["kind"]: n for n in family["nodes"]}
                assert by_kind["lane"]["parent_node_id"] == by_kind["process"]["node_id"]
                launches = [r for r in launches if r["kind"] == "lane"]
                assert launches[0]["parent_launch_id"] == "unit-parent"
            assert len(launches) == 1, (launches, result.stdout, result.stderr)
            launch, = launches
            assert launch["kind"] == ("teammate" if shape == "teammate" else "lane")
            assert launch["launcher_session_id"] == "family-parent"
            assert launch["launcher_profile"] == "default"
            assert launch["child_session_id"]
            assert launch["terminal_cause"] == {"thin_error": "error", "thin_stop": "user_stop"}.get(shape, "success"), (launch, result.stdout, result.stderr)
            assert launch["finished_at"] is not None
    finally:
        peer.release.set()
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)


def _run_cli(command, env, home, peer, desc, *, stop=False):
    if not stop:
        return subprocess.run(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=90, check=False)
    with subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        try:
            assert peer.blocked.wait(30), "finite CLI did not reach inference"
            with closing(sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True)) as db:
                sid, generation = db.execute("SELECT target_session_id,generation FROM session_admissions WHERE status='started'").fetchone()
            async def interrupt():
                async with websocket(home, desc) as ws:
                    reply = await rpc(ws, "session.interrupt", session_id=sid, execution_generation=generation)
                    assert "result" in reply, reply
            asyncio.run(interrupt())
            peer.release.set()
            stdout, stderr = proc.communicate(timeout=30)
            return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
        finally:
            peer.release.set()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
