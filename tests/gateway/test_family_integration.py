"""Real launch producers and CLI loops must agree with the public Family dispatchers."""
import asyncio
from contextlib import ExitStack, closing
import http.server
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

from hermes_state import SessionDB
from tests.hermes_cli.test_finite_chat_delegation import (
    test_finite_chat_joins_parallel_children_before_final_response as finite_chat,
)
from tests.gateway.fixtures.local_recovery_probe import Model, child_env, daemon
from tests.waiting import wait_until


REPO = Path(__file__).resolve().parents[2]


def identity(profile, value, category="session"):
    return json.dumps([profile, category, value], separators=(",", ":"))


def native_identity(authority, subject):
    return {"user_id": subject, "provider": "local", "profile_id": authority.profile_id,
            "instance_id": authority.instance_id, "profile_scope": "host",
            "capabilities": ["session:read", "session:submit", "session:control", "session:create",
                             "session:approve", "session:respond"]}


def test_parent_launch_index_reconciles_existing_store(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(path) as db:
        db._conn.execute("DROP INDEX IF EXISTS idx_work_launches_parent")
        db._conn.commit()
    with SessionDB(path) as db:
        assert "idx_work_launches_parent" in {row[1] for row in db._conn.execute("PRAGMA index_list(work_launches)")}
        plan = db._conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM work_launches WHERE parent_launch_id=?", ("job",)
        ).fetchall()
        assert any("idx_work_launches_parent" in row[3] for row in plan), plan


def test_real_producers_agree_with_family_dispatchers(tmp_path, monkeypatch):
    original_dumps, original_loads = json.dumps, json.loads
    offered_metadata = set()

    def read_request(value, *args, **kwargs):
        request = original_loads(value, *args, **kwargs)
        if isinstance(request, dict):
            for tool in request.get("tools", []):
                function = tool.get("function", {})
                if function.get("name") == "delegate_task":
                    offered_metadata.update(function.get("parameters", {}).get("properties", {}))
        return request

    def named_fanout(value, *args, **kwargs):
        if isinstance(value, dict) and "tasks" in value and len(value["tasks"]) == 2 and "batch_name" in offered_metadata:
            value = {**value, "batch_name": "Release proof", "tasks": [
                {**task, "title": title} for task, title in zip(value["tasks"], ("Alpha proof", "Beta proof"))
            ]}
        return original_dumps(value, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(json, "loads", read_request)
        patch.setattr(json, "dumps", named_fanout)
        finite_chat(tmp_path, "quiet")
    home = tmp_path / "profile"
    other = home / "profiles" / "iris"
    other.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: home)
    with SessionDB(home / "state.db") as db:
        roots = db._conn.execute("SELECT id FROM sessions WHERE source != 'subagent'").fetchall()
        assert len(roots) == 1, roots
        root = roots[0][0]
    asyncio.run(_assert_recorded_fanout(home, root))
    _teammate(tmp_path, other, root)
    from cron import jobs, executions
    from gateway.session_context import scoped_current_session_id
    with scoped_current_session_id(root):
        job = jobs.create_job("Release proof check-in", "every 1h", name="Release check-in")
        run = executions.create_execution(job["id"], source="manual")
        executions.mark_execution_running(run["id"])
        executions.finish_execution(run["id"], success=True)
        process = _process(tmp_path, monkeypatch, root)
    asyncio.run(_assert_rpc(home, other, root, job, run, process))


async def _assert_recorded_fanout(home, root):
    from gateway.session_authority import SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from hermes_state_runtime import begin_runtime_epoch
    with SessionDB(home / "state.db") as db:
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id=str(home),
            instance_id="fanout", db=db, epoch=begin_runtime_epoch(db, instance_id="fanout"))
        connection = AuthorityConnection(authority, object(), native_identity(authority, db.get_session(root)["user_id"]))
        try:
            response = await connection.dispatch({"id": 1, "method": "session.family", "params": {
                "session_id": root, "profile": "default", "since": 0}})
            assert "result" in response, response
            nodes = response["result"]["nodes"]
            assert {node["title"] for node in nodes} == {"Alpha proof", "Beta proof"}, nodes
            assert all(node["kind"] == "helper" and node["status"] == "done" for node in nodes)
            assert response["result"]["batches"][0]["batch_name"] == "Release proof"
        finally:
            await connection.close()


def _teammate(tmp_path, home, root):
    peer = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
    peer.requests = []
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{peer.server_port}/v1"
    config = {"gateway": {"multiplex_profiles": False},
              "model": {"provider": "custom", "default": "test-model", "base_url": url},
              "auxiliary": {"title_generation": {"enabled": False}},
              "memory": {"memory_enabled": False, "user_profile_enabled": False}}
    (home / "config.yaml").write_text(json.dumps(config))
    env = child_env()
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), HERMES_HOME=str(home),
               HERMES_MANAGED_DIR=str(tmp_path / "iris-managed"), PYTHONPATH=str(REPO),
               OPENAI_API_KEY="loopback-only", OPENAI_BASE_URL=url,
               HERMES_STARTED_BY_SESSION_ID=root, HERMES_STARTED_BY_PROFILE="default",
               HERMES_LAUNCH_KIND="teammate", HERMES_LAUNCH_TITLE="Iris release proof",
               HERMES_LAUNCH_ID="integration-iris")
    command = [sys.executable, "-c", "from hermes_cli.main import main; main()", "chat", "-Q",
               "--provider", "custom", "--model", "test-model", "--base-url", url,
               "--ignore-rules", "-q", "TEAMMATE_PROOF", "--max-turns", "2"]
    try:
        with daemon(REPO, home, env, barrier=False):
            result = subprocess.run(command, cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=75, check=False)
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert "RECOVERY_ACK_TEAMMATE_PROOF" in result.stdout
        assert peer.requests
    finally:
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)


def _process(tmp_path, monkeypatch, root):
    import tools.process_registry as module
    monkeypatch.setattr(module, "CHECKPOINT_PATH", tmp_path / "processes.json")
    registry = module.ProcessRegistry()
    monkeypatch.setattr(registry, "_scope_argv", lambda session, command, *args: ["bash", "-c", command])
    process = registry.spawn_local("printf FAMILY_PROCESS_PROOF", cwd=str(tmp_path),
                                   task_id=root, session_key=root)
    assert process.process.wait(timeout=10) == 0
    wait_until(lambda: process.exited, timeout=10, what="proof process completion")
    with SessionDB(tmp_path / "profile" / "state.db") as db:
        wait_until(lambda: db._read_one("SELECT terminal_cause FROM work_launches WHERE launch_id=?",
                                       (process.launch_id,))[0] == "success",
                   timeout=10, what="process terminal receipt")
    return process


async def _assert_rpc(home, other, root, job, run, process):
    from gateway.session_authorities import SessionAuthorities
    from gateway.session_authority import SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from hermes_state_runtime import begin_runtime_epoch
    registry = SessionAuthorities(home, multiplexed=True)
    runner = SimpleNamespace(_draining=False, session_authorities=registry)
    root_id = identity("default", root)
    with ExitStack() as stack:
        stores = {name: stack.enter_context(SessionDB(path / "state.db"))
                  for name, path in (("default", home), ("iris", other))}
        for name, path in (("default", home), ("iris", other)):
            db = stores[name]
            authority = SessionAuthority(runner, profile_id=str(path), instance_id=name, db=db,
                                         epoch=begin_runtime_epoch(db, instance_id=name))
            registry.add(path, authority, name=None if name == "default" else name)
            if name == "default":
                connection = AuthorityConnection(authority, object(), native_identity(authority, db.get_session(root)["user_id"]))
        try:
            response = await connection.dispatch({"id": 1, "method": "session.family", "params": {
                "session_id": root, "profile": "default", "since": 0}})
            assert "result" in response, response
            family = response["result"]
            assert len(family["nodes"]) == 6, family
            recorded = {}
            for name, db in stores.items():
                with closing(sqlite3.connect(f"file:{db.db_path}?mode=ro", uri=True)) as read:
                    read.row_factory = sqlite3.Row
                    recorded[name] = [dict(row) for row in read.execute("SELECT * FROM work_launches")]
            helpers = [row for row in recorded["default"] if row["kind"] == "helper"]
            assert len(helpers) == 2
            assert {row["title"] for row in helpers} == {"Alpha proof", "Beta proof"}
            assert {row["batch_name"] for row in helpers} == {"Release proof"}
            assert len({row["batch_id"] for row in helpers}) == 1
            assert all(row["launcher_session_id"] == root and row["launcher_profile"] == "default" for row in helpers)
            teammate, = recorded["iris"]
            assert (teammate["launch_id"], teammate["kind"], teammate["launcher_profile"],
                    teammate["launcher_session_id"], teammate["title"], teammate["terminal_cause"]) == (
                        "integration-iris", "teammate", "default", root, "Iris release proof", "success")
            expected = {}
            for profile, rows in recorded.items():
                for row in rows:
                    sid = row["child_session_id"]
                    key = identity(profile, sid or row["launch_id"], "session" if sid else "launch")
                    parent = identity(profile, row["parent_launch_id"], "launch") if row["parent_launch_id"] else root_id
                    expected[key] = {"node_id": key, "parent_node_id": parent, "kind": row["kind"],
                        "profile": profile, "session_id": sid, "title": row["title"], "batch_id": row["batch_id"],
                        "status": "unknown" if row["kind"] == "check_in" else "done",
                        "terminal_cause": None if row["kind"] == "check_in" else "success",
                        "backend_ref": row["backend_ref"] if row["kind"] in {"helper", "process", "check_in"} else None,
                        "root_session_id": root, "root_profile": "default"}
            assert len(expected) == 6
            assert {node["node_id"]: {key: node[key] for key in expected[node["node_id"]]}
                    for node in family["nodes"]} == expected
            assert family["complete"] is True and family["truncated"] is False
            assert family["unavailable_profiles"] == []
            assert {key: family["root"][key] for key in ("node_id", "parent_node_id", "kind", "profile", "session_id",
                "root_session_id", "root_profile", "child_count")} == {
                    "node_id": root_id, "parent_node_id": None, "kind": "chat", "profile": "default",
                    "session_id": root, "root_session_id": root, "root_profile": "default", "child_count": 5}
            assert family["batches"] == [{"batch_id": helpers[0]["batch_id"], "batch_name": "Release proof",
                "parent_node_id": root_id, "created_at": min(row["created_at"] for row in helpers), "size": 2}]
            by_kind = {node["kind"]: node for node in family["nodes"]}
            assert by_kind["process"]["backend_ref"] == process.id
            assert by_kind["check_in"]["backend_ref"] == job["id"]
            assert by_kind["check_in_run"]["node_id"] == identity("default", "cron-run:" + run["id"], "launch")
            assert by_kind["check_in_run"]["parent_node_id"] == by_kind["check_in"]["node_id"]
            assert by_kind["teammate"]["session_id"] == teammate["child_session_id"]
            overview = await connection.dispatch({"id": 2, "method": "family.overview", "params": {
                "since": 0, "coming_up_within": 7200}})
            assert "result" in overview, overview
            overview = overview["result"]
            assert {key: overview[key] for key in ("protocol_version", "team", "needs_you", "roots", "earlier", "failed",
                "complete", "unavailable_profiles")} == {"protocol_version": 1,
                    "team": [{"profile": "default", "state": "resting", "live_count": 0},
                             {"profile": "iris", "state": "resting", "live_count": 0}],
                    "needs_you": [], "roots": [], "earlier": [], "failed": [],
                    "complete": True, "unavailable_profiles": []}
            assert overview["coming_up"] == [by_kind["check_in"]]
            assert all(not authority.sessions for authority in (connection.authority,))
        finally:
            await connection.close()
