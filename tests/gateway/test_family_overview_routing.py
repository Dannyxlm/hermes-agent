"""Overview's routing envelope is strict and never grants another profile or principal."""
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB


@pytest.mark.asyncio
@pytest.mark.parametrize("host_scoped", [False, True])
async def test_overview_profile_envelope_uses_real_sibling_dispatch(tmp_path, monkeypatch, host_scoped):
    from gateway.session_authorities import SessionAuthorities
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
            store.create_session(f"{name}-private", source="local", user_id="somebody-else")
            authority = SessionAuthority(runner, profile_id=str(path), instance_id=name, db=store,
                epoch=begin_runtime_epoch(store, instance_id=name))
            registry.add(path, authority, name=None if name == "default" else name)
            authorities.append(authority)
        connection = AuthorityConnection(authorities[0], object(), {
            "user_id": "owner", "profile_scope": "host" if host_scoped else "profile"})
        try:
            own = await connection.dispatch({"id": 1, "method": "family.overview", "params": {"profile": "default", "since": 0}})
            assert "result" in own, own
            routed = await connection.dispatch({"id": 2, "method": "family.overview", "params": {"profile": "reviewer", "since": 0}})
            assert ("result" in routed) is host_scoped, routed
            if host_scoped:
                assert {row["profile"] for row in routed["result"]["team"]} == {"default", "reviewer"}
                assert "private" not in str(routed["result"])
            for params in ({"profile": "default", "typo": True}, {"profile": 7}, {"profile": "missing"}):
                invalid = await connection.dispatch({"id": 3, "method": "family.overview", "params": params})
                assert "error" in invalid, invalid
        finally:
            await connection.close()
