"""Native REST read mutations synchronize compression watermarks after owner checks."""

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_inbox_summaries import inbox, patch_sidebar, row
from tests.tui_gateway.test_methods_mobile import mobile_home, peer, rpc
from tui_gateway import server


@pytest.fixture
def read_client(inbox):
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
    monkeypatch.setattr("hermes_state_mutation_read.time.time", lambda: 200)
    for index, (sid, unread) in enumerate([("tip", True), ("same", False), ("same", True), ("tip", False)]):
        response = patch_sidebar(read_client, sid, request_id=f"read-{index}", unread=unread)
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
    response = patch_sidebar(read_client, "tip", request_id="foreign-read", unread=False,
                             title="must not rename")
    assert response.status_code == 404
    with SessionDB(db_path=home / "state.db", read_only=True) as db:
        assert db.get_session("same")["last_read_at"] is None
        assert db.get_session("tip")["last_read_at"] is None
        assert db.get_session_title("same") == "Ordinary"

@pytest.fixture
def replied_family(mobile_home):
    """root -> tip (compression), a branch beside it; replies at t=100 (root), 150 and 250 (tip)."""
    home = mobile_home / "profiles" / "ops"
    with SessionDB(db_path=home / "state.db") as db:
        db.append_message("same", "assistant", "root reply", timestamp=100)
        db._conn.execute("UPDATE sessions SET end_reason='compression', ended_at=101 WHERE id='same'")
        db.create_session("tip", "desktop", parent_session_id="same", profile_name="ops")
        db.set_session_title("tip", "Ordinary")
        seen = db.append_message("tip", "assistant", "seen reply", timestamp=150)
        db.append_message("tip", "assistant", "landed while leaving", timestamp=250)
        db.create_session("branch", "desktop", parent_session_id="same",
                          model_config={"_branched_from": "same"}, profile_name="ops")
        elsewhere = db.append_message("branch", "assistant", "branch reply", timestamp=160)
        future = db.append_message("tip", "assistant", "from the future", timestamp=9000)
    server._sessions["ops"]["session_key"] = "tip"
    return home, {"seen": seen, "elsewhere": elsewhere, "future": future}


def _stamps(home):
    with SessionDB(db_path=home / "state.db", read_only=True) as db:
        return {sid: db.get_session(sid)["last_read_at"] for sid in ("same", "tip", "branch")}


def test_read_through_stamps_the_displayed_reply_not_the_clock(read_client, replied_family, monkeypatch):
    home, rows = replied_family
    monkeypatch.setattr("hermes_state_mutation_read.time.time", lambda: 300)
    response = patch_sidebar(read_client, "tip", request_id="through", unread=False, read_through_row_id=rows["seen"])
    assert response.status_code == 200, response.text
    assert _stamps(home) == {"same": 150, "tip": 150, "branch": None}
    replies = read_client.get("/api/sessions", params={"profile": "ops"}).json()["sessions"]
    unread = next(r for r in replies if r["id"] == "tip")["last_assistant_reply"]
    assert unread["unread"] is True and unread["at"] == 9000  # the reply that landed later stays unread


def test_read_through_never_stamps_past_now(read_client, replied_family, monkeypatch):
    home, rows = replied_family
    monkeypatch.setattr("hermes_state_mutation_read.time.time", lambda: 300)
    assert patch_sidebar(read_client, "tip", request_id="future", unread=False,
                         read_through_row_id=rows["future"]).status_code == 200
    assert _stamps(home)["tip"] == 300


@pytest.mark.parametrize("which", ["elsewhere", "missing"])
def test_read_through_row_outside_the_lineage_behaves_like_a_plain_read(read_client, replied_family,
                                                                         monkeypatch, which):
    home, rows = replied_family
    monkeypatch.setattr("hermes_state_mutation_read.time.time", lambda: 300)
    row_id = rows["elsewhere"] if which == "elsewhere" else 10**9
    assert patch_sidebar(read_client, "tip", request_id="foreign", unread=False,
                         read_through_row_id=row_id).status_code == 200
    assert _stamps(home) == {"same": 300, "tip": 300, "branch": None}


def test_marking_unread_ignores_read_through(read_client, replied_family):
    home, rows = replied_family
    assert patch_sidebar(read_client, "tip", request_id="unread", unread=True,
                         read_through_row_id=rows["seen"]).status_code == 200
    assert _stamps(home)["tip"] == 0


def test_a_read_never_lowers_the_watermark(read_client, replied_family, monkeypatch):
    """Two devices (or a cached open) read through an older reply after a newer one was read: still read."""
    home, rows = replied_family
    with SessionDB(db_path=home / "state.db") as db:
        newest = db.append_message("tip", "assistant", "newest", timestamp=260)
    monkeypatch.setattr("hermes_state_mutation_read.time.time", lambda: 300)
    for index, row_id in enumerate((newest, rows["seen"])):
        assert patch_sidebar(read_client, "tip", request_id=f"through-{index}", unread=False,
                             read_through_row_id=row_id).status_code == 200
        assert _stamps(home) == {"same": 260, "tip": 260, "branch": None}
    replies = read_client.get("/api/sessions", params={"profile": "ops"}).json()["sessions"]
    assert next(r for r in replies if r["id"] == "tip")["last_assistant_reply"]["unread"] is False
    assert patch_sidebar(read_client, "tip", request_id="unread", unread=True).status_code == 200
    assert _stamps(home)["tip"] == 0  # marking unread still lowers it
