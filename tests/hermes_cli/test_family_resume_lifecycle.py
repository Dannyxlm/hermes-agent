"""Finite thin CLI lifecycle against a disposable real gateway and loopback inference."""
import asyncio
from contextlib import closing
from http.server import ThreadingHTTPServer
import json
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


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    peer = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    peer.requests = []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    user, home = tmp_path / "user", tmp_path / "state"
    user.mkdir()
    home.mkdir()
    url = f"http://127.0.0.1:{peer.server_port}/v1"
    config = {"gateway": {"multiplex_profiles": False},
              "model": {"provider": "custom", "base_url": url, "default": "test-model", "api_mode": "chat_completions"},
              "auxiliary": {"title_generation": {"enabled": False}},
              "memory": {"memory_enabled": False, "user_profile_enabled": False},
              "platform_toolsets": {"cli": []}}
    (home / "config.yaml").write_text(json.dumps(config))
    with SessionDB(home / "state.db") as db:
        db.create_session("family-parent", "cli", profile_name="default")
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home),
               OPENAI_API_KEY="loopback-only", OPENAI_BASE_URL=url, PYTHONPATH=str(ROOT),
               HERMES_MANAGED_DIR=str(tmp_path / "managed"), TERMINAL_CWD=str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    tokens = set_session_vars(session_id="family-parent", profile="default")
    try:
        terminal = _make_run_env({})
    finally:
        clear_session_vars(tokens)
    cli_env = {**env, **{k: v for k, v in terminal.items() if k.startswith("HERMES_STARTED_BY_")}}
    try:
        with daemon(ROOT, home, env, barrier=False) as (_, desc):
            yield home, peer, desc, env, cli_env
    finally:
        peer.release.set()
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)


def command(*args):
    return [sys.executable, "-c", "from hermes_cli.main import main; main()", "chat", "--format", "stream-json", *args]


def run(env, *args):
    result = subprocess.run(command(*args), cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=90, check=False)
    assert result.returncode == 0, (result.stdout, result.stderr)
    return result


def stored(home, query, params=()):
    with closing(sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(query, params)]


async def family(home, desc):
    async with websocket(home, desc) as ws:
        reply = await rpc(ws, "session.family", profile="default", session_id="family-parent", since=0)
        assert "result" in reply, reply
        return reply["result"]


def test_second_cli_resume_reopens_lane_until_receipt_close(runtime):
    home, peer, desc, _, env = runtime
    env = {**env, "HERMES_LAUNCH_ID": "resumable-lane"}
    run(env, "--oneshot", "-q", "initial", "-Q")
    launch, = stored(home, "SELECT * FROM work_launches")
    sid = launch["child_session_id"]
    assert launch["terminal_cause"] == "success" and launch["finished_at"] is not None
    run(env, "--resume", sid, "--oneshot", "-q", "first resume", "-Q")
    with subprocess.Popen(command("--resume", sid, "--oneshot", "-q", "BLOCK_STARTED", "-Q"),
                          cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        try:
            assert peer.blocked.wait(30), "second resume never reached inference"
            leases = stored(home, "SELECT * FROM session_turn_leases")
            assert leases, "gateway has no live turn lease"
            snapshot = asyncio.run(family(home, desc))
            lane, = [n for n in snapshot["nodes"] if n["session_id"] == sid]
            assert lane["status"] == "working", lane
            assert lane["terminal_cause"] is None and lane["ended_at"] is None
            peer.release.set()
            out, err = proc.communicate(timeout=40)
            assert proc.returncode == 0, (out, err)
        finally:
            peer.release.set()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
    lane, = [n for n in asyncio.run(family(home, desc))["nodes"] if n["session_id"] == sid]
    assert lane["status"] == "done" and lane["terminal_cause"] == "success"
    assert lane["ended_at"] is not None


@pytest.mark.parametrize("source", ["created", "oneshot", "tool", "tui", "cli", "gui", "bot_chat"])
def test_finite_cli_closes_only_owned_ephemeral_sessions(runtime, source):
    home, _, desc, _, attributed = runtime
    if source == "created":
        run(attributed, "--oneshot", "-q", "created", "-Q")
        launch, = stored(home, "SELECT * FROM work_launches")
        sid = launch["child_session_id"]
    else:
        async def create():
            async with websocket(home, desc) as ws:
                reply = await rpc(ws, "session.create", request_id="initial", source="cli" if source == "bot_chat" else source,
                                  cwd=str(home), toolsets=[], **({"title": "Bot Chat"} if source == "bot_chat" else {}))
                assert "result" in reply, reply
                return reply["result"]["stored_session_id"]
        sid = asyncio.run(create())
        row, = stored(home, "SELECT * FROM sessions WHERE id=?", (sid,))
        assert row["ended_at"] is None
        resume = ["-c", "Bot Chat", "--create-if-missing"] if source == "bot_chat" else ["--resume", sid]
        run(attributed, *resume, "-q", "resumed", "-Q", *(["--oneshot"] if source in {"oneshot", "tool"} else []))
    row, = stored(home, "SELECT * FROM sessions WHERE id=?", (sid,))
    launches = stored(home, "SELECT * FROM work_launches")
    from hermes_state_terminal import RESULT_PREFIX, load_result
    receipt, = stored(home, "SELECT m.value FROM session_admissions a JOIN state_meta m ON m.key=? || a.admission_id "
                           "WHERE a.target_session_id=? ORDER BY a.seq DESC LIMIT 1", (RESULT_PREFIX, sid))
    assert load_result(receipt["value"])["result"]["terminal_cause"] == "success"
    if source in {"created", "oneshot", "tool"}:
        assert row["end_reason"] == "cli_close" and row["ended_at"] is not None
        launch, = launches
        assert launch["child_session_id"] == sid and launch["terminal_cause"] == "success"
        assert launch["finished_at"] is not None
    else:
        assert row["ended_at"] is None and row["end_reason"] is None, (row["ended_at"], row["end_reason"], launches)
        assert launches == [], launches
