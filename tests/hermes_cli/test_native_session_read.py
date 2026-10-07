"""Native REST read mutations synchronize compression watermarks after owner checks."""

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_inbox_summaries import inbox, row
from tests.tui_gateway.test_methods_mobile import mobile_home, peer, rpc
from tui_gateway import server


@pytest.fixture
def read_client(inbox):
    from hermes_cli.web_routers import sessions
    inbox.app.include_router(sessions.manage_router)
    return inbox


def test_native_read_unread_roundtrip_stamps_root_tip_not_branch(read_client, mobile_home, peer, monkeypatch):
    home = mobile_home / "profiles" / "ops"
    with SessionDB(db_path=home / "state.db") as db:
        db.append_message("same", "assistant", "root reply", timestamp=100)
        db._conn.execute("UPDATE sessions SET end_reason='compression', ended_at=101 WHERE id='same'")
        db.create_session("tip", "desktop", parent_session_id="same", profile_name="ops")
        db.set_session_title("tip", "Ordinary")
        reply_id = db.append_message("tip", "assistant", "tip reply", timestamp=150)
        db.create_session("branch", "desktop", parent_session_id="same",
                          model_config={"_branched_from": "same"}, profile_name="ops")
    server._sessions["ops"]["session_key"] = "tip"
    monkeypatch.setattr("hermes_state_sessions.time.time", lambda: 200)
    for sid, unread in [("tip", True), ("same", False), ("same", True), ("tip", False)]:
        response = read_client.patch(f"/api/sessions/{sid}", json={"profile": "ops", "unread": unread})
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert response.json()["unread"] is unread
        with SessionDB(db_path=home / "state.db", read_only=True) as db:
            assert db.get_session("same")["last_read_at"] == (0 if unread else 200)
            assert db.get_session("tip")["last_read_at"] == (0 if unread else 200)
            assert db.get_session("branch")["last_read_at"] is None
        rest = read_client.get("/api/sessions", params={"profile": "ops"}).json()
        reply = next(r for r in rest["sessions"] if r["id"] == "tip")["last_assistant_reply"]
        assert reply == {"row_id": reply_id, "at": 150, "preview": "tip reply", "unread": unread}
        live = rpc("session.active_list", profile="ops")["result"]["sessions"][0]
        assert live["last_assistant_reply"] == reply
        assert "last_assistant_reply" not in row(read_client, "default")
    with SessionDB(db_path=home / "state.db") as db:
        newer_id = db.append_message("tip", "assistant", "new completion", timestamp=201)
    server._sessions["ops"]["running"] = True
    reply = rpc("session.active_list", profile="ops")["result"]["sessions"][0]["last_assistant_reply"]
    assert reply == {"row_id": newer_id, "at": 201, "preview": "new completion", "unread": True}
    rest = read_client.get("/api/sessions", params={"profile": "ops"}).json()
    assert next(r for r in rest["sessions"] if r["id"] == "tip")["last_assistant_reply"] == reply


@pytest.mark.parametrize("foreign_segment", ["same", "tip"])
def test_native_read_rejects_foreign_owner_before_any_mutation(read_client, mobile_home, foreign_segment):
    home = mobile_home / "profiles" / "ops"
    with SessionDB(db_path=home / "state.db") as db:
        db._conn.execute("UPDATE sessions SET end_reason='compression', ended_at=101, profile_name='ops' "
                         "WHERE id='same'")
        db.create_session("tip", "desktop", parent_session_id="same", profile_name="ops")
        db._conn.execute("UPDATE sessions SET profile_name='default' WHERE id=?", (foreign_segment,))
    response = read_client.patch("/api/sessions/tip", json={"profile": "ops", "unread": False,
                                                          "title": "must not rename"})
    assert response.status_code == 404
    with SessionDB(db_path=home / "state.db", read_only=True) as db:
        assert db.get_session("same")["last_read_at"] is None
        assert db.get_session("tip")["last_read_at"] is None
        assert db.get_session_title("same") == "Ordinary"