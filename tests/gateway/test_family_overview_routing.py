"""Overview's routing envelope is strict and never grants another profile or principal."""
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tests.test_hermes_family import LAUNCH_DDL, NOW, launch


@pytest.mark.asyncio
@pytest.mark.parametrize("host_scoped", [False, True])
async def test_overview_profile_envelope_uses_real_sibling_dispatch(tmp_path, monkeypatch, host_scoped):
    from gateway.session_authorities import SessionAuthorities
    from gateway.owner_identity import principal_subject
    from gateway.session_authority import SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from hermes_state_runtime import begin_runtime_epoch
    home = tmp_path / "home"
    secondary = home / "profiles" / "reviewer"
    secondary.mkdir(parents=True)
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: home)
    registry = SessionAuthorities(home, multiplexed=True)
    runner = SimpleNamespace(_draining=False, session_authorities=registry)
    with SessionDB(db_path=home / "state.db") as db, SessionDB(db_path=secondary / "state.db") as other:
        authorities = []
        for path, store, name in ((home, db, "default"), (secondary, other, "reviewer")):
            store.create_session(f"{name}-public", source="test")
            store.create_session(f"{name}-private", source="local", user_id=principal_subject({"provider": "local", "user_id": "somebody-else"}))
            store._conn.executescript(LAUNCH_DDL)
            for visibility in ("public", "private"):
                root = f"{name}-{visibility}"
                child = f"{root}-helper"
                store.create_session(child, source="subagent", parent_session_id=root,
                                     user_id=principal_subject({"provider": "local", "user_id": "somebody-else"}) if visibility == "private" else None)
                launch(store._conn, child, profile=name, launcher=root, launcher_profile=name, cause="success", finished=NOW - 1)
            store._conn.commit()
            authority = SessionAuthority(runner, profile_id=str(path), instance_id=name, db=store,
                epoch=begin_runtime_epoch(store, instance_id=name))
            registry.add(path, authority, name=None if name == "default" else name)
            authorities.append(authority)
        connection = AuthorityConnection(authorities[0], object(), {
            "user_id": "owner", "profile_scope": "host" if host_scoped else "profile"})
        try:
            own = await connection.dispatch({"id": 1, "method": "family.overview", "params": {"profile": "default", "since": 0}})
            assert "result" in own, own
            roots = {row["root"]["session_id"] for row in own["result"]["earlier"]}
            assert "default-public" in roots
            assert not any("private" in root for root in roots)
            routed = await connection.dispatch({"id": 2, "method": "family.overview", "params": {"profile": "reviewer", "since": 0}})
            assert ("result" in routed) is host_scoped, routed
            if host_scoped:
                assert {row["profile"] for row in routed["result"]["team"]} == {"default", "reviewer"}
                roots = {row["root"]["session_id"] for row in routed["result"]["earlier"]}
                assert {"default-public", "reviewer-public"} <= roots
                assert not any("private" in root for root in roots)
            foreign = AuthorityConnection(authorities[0], object(), {
                "provider": "local", "user_id": "somebody-else", "profile_scope": "host" if host_scoped else "profile"})
            try:
                allowed = await foreign.dispatch({"id": 4, "method": "family.overview", "params": {"profile": "default", "since": 0}})
                assert "result" in allowed, allowed
                assert "default-private" in {row["root"]["session_id"] for row in allowed["result"]["earlier"]}
            finally:
                await foreign.close()
            for params in ({"profile": "default", "typo": True}, {"profile": 7}, {"profile": "missing"}):
                invalid = await connection.dispatch({"id": 3, "method": "family.overview", "params": params})
                assert "error" in invalid, invalid
        finally:
            await connection.close()
