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
from tests.gateway.fixtures.local_recovery_probe import Model, child_env, daemon
from tools.environments.local import _make_run_env

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("shape", ["query_file", "quiet", "unattended", "thin_error", "teammate"])
def test_terminal_finite_cli_launch(tmp_path, monkeypatch, shape):
    class Provider(Model):
        def do_POST(self):
            if shape == "thin_error":
                self.send_error(400, "Deliberate inference rejection")
            else:
                super().do_POST()

    peer = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    peer.requests = []
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
    args = ["chat", "-q", "hello", "-Q"]
    if shape == "query_file":
        query = tmp_path / "query.txt"
        query.write_text("hello")
        args = ["chat", "--query-file", str(query), "--oneshot", "-Q"]
    elif shape in {"unattended", "thin_error"}:
        args = ["-z", "hello"]
    elif shape == "teammate":
        args = ["-p", "atlas", *args]
    # Explicit profile selection must resolve under the caller's HOME.
    cli_env = {**env, "HERMES_HOME": str(home)}
    daemon_env = dict(env)
    for key in list(daemon_env):
        if key.startswith("HERMES_STARTED_BY_"):
            daemon_env.pop(key)
    try:
        with daemon(ROOT, target, daemon_env, barrier=False):
            result = subprocess.run([sys.executable, "-c", "from hermes_cli.main import main; main()", *args],
                                    cwd=ROOT, env=cli_env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=90)
            assert result.returncode == (2 if shape == "thin_error" else 0), (result.stdout, result.stderr)
            with closing(sqlite3.connect(f"file:{target / 'state.db'}?mode=ro", uri=True)) as db:
                db.row_factory = sqlite3.Row
                launches = [dict(r) for r in db.execute("SELECT * FROM work_launches")]
            assert len(launches) == 1, (launches, result.stdout, result.stderr)
            launch, = launches
            assert launch["kind"] == ("teammate" if shape == "teammate" else "lane")
            assert launch["launcher_session_id"] == "family-parent"
            assert launch["launcher_profile"] == "default"
            assert launch["child_session_id"]
            assert launch["terminal_cause"] == ("error" if shape == "thin_error" else "success"), (launch, result.stdout, result.stderr)
            assert launch["finished_at"] is not None
    finally:
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)
