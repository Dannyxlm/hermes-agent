"""Parent-launch intake and operator Family lineage keep CLI ownership unchanged."""
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from gateway.session_controls import AuthorityConnection
from hermes_family import Family, read_profile
from hermes_state import SessionDB
from tests.gateway.test_family_integration import identity, native_identity


def test_parent_launch_environment_intake(monkeypatch):
    from hermes_cli.launch_provenance import launch_record, take_launch
    monkeypatch.setenv("HERMES_STARTED_BY_SESSION_ID", "launcher")
    monkeypatch.setenv("HERMES_STARTED_BY_PROFILE", "default")
    monkeypatch.setenv("HERMES_LAUNCH_PARENT_ID", "unit-launch")
    fields = take_launch()
    assert launch_record(fields, "default")["parent_launch_id"] == "unit-launch"


@pytest.mark.asyncio
async def test_gateway_parent_launch_intake_nests_under_unit(tmp_path, monkeypatch):
    from tests.gateway.test_local_source_labels import _authority
    authority, _ = await _authority(tmp_path, monkeypatch)
    conn = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None), {"user_id": "cli"}, operator=True)
    db = authority.db
    db.create_session("launcher", "cli")
    db.record_launch("unit-launch", kind="process", launcher_profile="default", launcher_session_id="launcher",
                     child_profile="default", backend_ref="systemd:example")
    try:
        created = await conn.dispatch({"id": 1, "method": "session.create", "params": {
            "request_id": "nested", "source": "oneshot", "cwd": str(tmp_path),
            "started_by_session": "launcher", "started_by_profile": "default",
            "launch_id": "lane", "parent_launch_id": "unit-launch"}})
        assert "result" in created, created
        sid = created["result"]["stored_session_id"]
        assert db._read_one("SELECT parent_launch_id FROM work_launches WHERE launch_id='lane'")[0] == "unit-launch"
        result = await conn.dispatch({"id": 2, "method": "session.family", "params": {
            "session_id": "launcher", "profile": "default"}})
        assert "result" in result, result
        nodes = {n["kind"]: n for n in result["result"]["nodes"]}
        assert nodes["lane"]["session_id"] == sid
        assert nodes["lane"]["parent_node_id"] == nodes["process"]["node_id"]
    finally:
        await conn.close()
        db.close()


@pytest.mark.asyncio
async def test_operator_sees_cli_lane_and_teammate_with_missing_unit_parent(tmp_path, monkeypatch):
    from gateway.session_authorities import SessionAuthorities
    from gateway.session_authority import SessionAuthority
    from hermes_state_runtime import begin_runtime_epoch
    home, other = tmp_path / "state", tmp_path / "state/profiles/atlas"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: home)
    registry = SessionAuthorities(home, multiplexed=True)
    runner = SimpleNamespace(_draining=False, session_authorities=registry)
    with ExitStack() as stack:
        for name, path in (("default", home), ("atlas", other)):
            path.mkdir(parents=True, exist_ok=True)
            db = stack.enter_context(SessionDB(path / "state.db"))
            db.create_session(name + "-child", "oneshot", user_id="cli-principal")
            db.record_launch(name + "-launch", kind="lane" if name == "default" else "teammate",
                             launcher_profile="default", launcher_session_id="launcher", child_profile=name,
                             parent_launch_id="invisible-unit")
            db.bind_launch(name + "-launch", child_session_id=name + "-child")
            if name == "default":
                db.create_session("launcher", "cli", user_id="phone-principal")
            authority = SessionAuthority(runner, profile_id=str(path), instance_id=name, db=db,
                                         epoch=begin_runtime_epoch(db, instance_id=name))
            registry.add(path, authority, name=None if name == "default" else name)
            if name == "default":
                conn = AuthorityConnection(authority, object(), native_identity(authority, "phone-principal"), operator=True)
        try:
            reply = await conn.dispatch({"id": 1, "method": "session.family", "params": {
                "session_id": "launcher", "profile": "default"}})
            assert "result" in reply, reply
            nodes = reply["result"]["nodes"]
            assert {(n["kind"], n["profile"]) for n in nodes} == {("lane", "default"), ("teammate", "atlas")}
            assert all(n["parent_node_id"] == identity("default", "launcher") for n in nodes)
        finally:
            await conn.close()


def test_missing_parent_falls_back_to_launcher(tmp_path):
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("launcher", "cli")
        db.create_session("child", "oneshot")
        db.record_launch("child-launch", kind="lane", launcher_profile="default", launcher_session_id="launcher",
                         child_profile="default", parent_launch_id="not-visible")
        db.bind_launch("child-launch", child_session_id="child")
    family = Family({"default": read_profile("default", tmp_path / "state.db")}).session_family("default", "launcher")
    node, = family["nodes"]
    assert node["parent_node_id"] == family["root"]["node_id"]
