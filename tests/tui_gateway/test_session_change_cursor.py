"""Sessions change cursor (Hermex R9 U11, KTD5): per-scope cursor frames for cursor-aware clients,
``changed_since`` delta reads on the REST and RPC lists, and an unchanged legacy contract.

Exercised against real profile stores and the real watcher pass; only the clock is synthetic.
"""

import os
import shutil
import time

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_inbox_summaries import inbox  # noqa: F401 - fixture
from tests.tui_gateway.test_methods_mobile import mobile_home, rpc  # noqa: F401 - fixture
from tui_gateway import server, server_requests, session_change_cursor

PHONE_EXCLUDES = "acp,cron,kanban,oneshot,subagent,tool,telegram"
LEGACY_KEYS = {"sessions", "total", "limit", "offset", "archived_count", "storage", "inbox_summary_scope"}


class Peer:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)
        return True

    def payloads(self, event="sessions.changed"):
        return [f["params"].get("payload") for f in self.frames if f.get("params", {}).get("type") == event]


@pytest.fixture(autouse=True)
def clean_journal(monkeypatch):
    session_change_cursor.reset_for_tests()
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    yield
    session_change_cursor.reset_for_tests()


def _ops_db(home):
    return home / "profiles" / "ops" / "state.db"


def _write(db_path, fn):
    time.sleep(0.01)  # distinct mtimes are not required (sizes guard too), but keep writes ordered
    with SessionDB(db_path=db_path) as db:
        return fn(db)


def _list(client, **params):
    query = {"profile": "ops", "order": "recent", "exclude_sources": PHONE_EXCLUDES, **params}
    response = client.get("/api/sessions", params=query)
    assert response.status_code == 200, response.text
    return response.json()


# ── watcher: cursor frames vs legacy frames ───────────────────────────────────────────────────

@pytest.fixture
def watched(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("display: {}\n")
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(server, "_hermes_home", str(home))
    monkeypatch.setattr(server, "_cfg_cache", None)
    for name in ("_change_sigs", "_change_checked_at", "_change_broadcast_at"):
        monkeypatch.setattr(server, name, {})
    monkeypatch.setattr(server, "_served_profile_homes", set())
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session("chat", "desktop")
        db.append_message("chat", "user", "hello")
    legacy, cursor = Peer(), Peer()
    monkeypatch.setattr(server, "_live_transports", {legacy, cursor})
    session_change_cursor.subscribe(cursor, True)
    return home, legacy, cursor


def test_agent_write_emits_one_cursor_frame_within_250ms(watched):
    home, legacy, cursor = watched
    server._broadcast_watched_changes(now=0.0)  # first sighting seeds silently
    assert cursor.frames == [] and legacy.frames == []

    _write(home / "state.db", lambda db: db.append_message("chat", "assistant", "reply", finish_reason="stop"))
    server._broadcast_watched_changes(now=0.25)

    [payload] = cursor.payloads()
    assert payload["changed"] == ["chat"] and payload["tombstoned"] == []
    assert payload["profile"] == "default"
    assert payload["cursor"].startswith("sc1.")
    # The legacy client still gets today's payload-free frame; the cursor client never does.
    assert legacy.payloads() == [{}]
    assert all(p.get("cursor") for p in cursor.payloads())


def test_cursor_clients_coalesce_at_250ms_while_legacy_keeps_2s_floor(watched):
    home, legacy, cursor = watched
    server._broadcast_watched_changes(now=0.0)
    for tick in range(1, 9):  # a write every 250 ms for 2 s
        _write(home / "state.db", lambda db: db.append_message("chat", "tool", f"t{tick}", tool_name="x"))
        server._broadcast_watched_changes(now=tick * 0.25)
    cursors = [p["cursor"] for p in cursor.payloads()]
    assert len(cursors) == 8 and len(set(cursors)) == 8
    assert len(legacy.payloads()) == 1  # at most one per 2 s window


def test_watch_interval_and_frames_follow_subscription(watched, monkeypatch):
    home, legacy, cursor = watched
    assert session_change_cursor.watch_interval(0.5) == 0.25
    session_change_cursor.subscribe(cursor, False)
    assert session_change_cursor.watch_interval(0.5) == 0.5
    server._broadcast_watched_changes(now=0.0)
    _write(home / "state.db", lambda db: db.set_session_title("chat", "Renamed"))
    server._broadcast_watched_changes(now=10.0)
    assert cursor.payloads() == [{}]  # unsubscribed: back to the legacy frame


def test_disconnected_subscriber_is_pruned(watched, monkeypatch):
    home, legacy, cursor = watched
    monkeypatch.setattr(server, "_live_transports", {legacy})
    server._broadcast_watched_changes(now=0.0)
    assert not session_change_cursor.is_subscribed(cursor)


def test_internal_sources_and_delegate_runs_never_wake_cursor_clients(watched):
    home, legacy, cursor = watched
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session("worker", "kanban")
        db.create_session("child", "desktop", parent_session_id="chat", model_config={"_delegate_from": "chat"})
    server._broadcast_watched_changes(now=0.0)
    _write(home / "state.db", lambda db: db.append_message("worker", "assistant", "kanban", finish_reason="stop"))
    _write(home / "state.db", lambda db: db.append_message("child", "assistant", "delegated", finish_reason="stop"))
    server._broadcast_watched_changes(now=0.25)
    assert cursor.payloads() == []
    assert legacy.payloads() == [{}]  # Desktop keeps today's contract


def test_cron_changes_are_quiet_until_a_loud_change(watched):
    home, legacy, cursor = watched
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session("cron-run", "cron")
    server._broadcast_watched_changes(now=0.0)
    _write(home / "state.db", lambda db: db.append_message("cron-run", "assistant", "tick", finish_reason="stop"))
    server._broadcast_watched_changes(now=0.25)
    assert cursor.payloads() == []
    _write(home / "state.db", lambda db: db.set_session_title("chat", "Loud"))
    server._broadcast_watched_changes(now=0.5)
    [payload] = cursor.payloads()
    assert payload["changed"] == ["chat", "cron-run"]


def test_real_global_broadcast_skips_cursor_clients(monkeypatch):
    legacy, cursor = Peer(), Peer()
    monkeypatch.setattr(server, "_live_transports", {legacy, cursor})
    session_change_cursor.subscribe(cursor, True)
    server._broadcast_global_event("sessions.changed", {})
    server._broadcast_global_event("cron.changed", {})
    assert legacy.payloads() == [{}] and cursor.payloads() == []
    assert len(cursor.payloads("cron.changed")) == 1


# ── REST changed_since ────────────────────────────────────────────────────────────────────────

def test_desktop_list_response_is_unchanged_without_new_arguments(inbox):
    body = inbox.get("/api/sessions", params={"profile": "ops"}).json()
    assert set(body) == LEGACY_KEYS
    assert set(rpc("session.list", profile="ops")["result"]) == {"sessions"}


def test_changed_since_returns_only_changed_rows_plus_tombstones(inbox, mobile_home):
    db_path = _ops_db(mobile_home)
    _write(db_path, lambda db: (db.create_session("quiet", "desktop"), db.set_session_title("quiet", "Quiet")))
    full = _list(inbox, change_cursor=1)
    assert {row["id"] for row in full["sessions"]} >= {"same", "quiet"}
    first = full["change_cursor"]
    assert first

    _write(db_path, lambda db: db.append_message("same", "assistant", "new reply", finish_reason="stop"))
    _write(db_path, lambda db: (db.create_session("fresh", "desktop"), db.set_session_title("fresh", "Fresh")))
    delta = _list(inbox, changed_since=first)
    assert delta["delta"] is True and delta["repair"] is False
    assert sorted(row["id"] for row in delta["sessions"]) == ["fresh", "same"]
    assert delta["tombstones"] == []
    same = next(row for row in delta["sessions"] if row["id"] == "same")
    # Served atomically with the Inbox summary fields the full list carries.
    assert same["last_assistant_reply"]["preview"] == "new reply"
    assert delta["inbox_summary_scope"]["profile"] == "ops"
    second = delta["change_cursor"]
    assert second != first

    assert _list(inbox, changed_since=second)["sessions"] == []  # nothing new: empty delta
    _write(db_path, lambda db: db.delete_session("fresh"))
    _write(db_path, lambda db: db.set_session_archived("same", True))
    gone = _list(inbox, changed_since=second)
    assert gone["sessions"] == [] and gone["tombstones"] == ["fresh", "same"]
    # The same cursor under a filter that still lists archived rows returns the row instead.
    kept = _list(inbox, changed_since=second, archived="include")
    assert [row["id"] for row in kept["sessions"]] == ["same"] and kept["tombstones"] == ["fresh"]


def test_compression_returns_the_new_tip_and_tombstones_the_old_row(inbox, mobile_home):
    db_path = _ops_db(mobile_home)
    _write(db_path, lambda db: (db.create_session("lineage", "desktop"), db.set_session_title("lineage", "Long"),
                                db.append_message("lineage", "user", "hi")))
    cursor = _list(inbox, change_cursor=1)["change_cursor"]

    def compress(db):
        db.end_session("lineage", "compression")
        db.create_session("lineage-2", "desktop", parent_session_id="lineage")
        db.append_message("lineage-2", "user", "continued")
    _write(db_path, compress)
    delta = _list(inbox, changed_since=cursor)
    assert [row["id"] for row in delta["sessions"]] == ["lineage-2"]
    assert delta["sessions"][0]["_lineage_root_id"] == "lineage"
    assert delta["tombstones"] == ["lineage"]

    # A later write touches only the tip, which is never a listing row by itself: the lineage
    # must still come back as its projected row, not as a tombstone.
    _write(db_path, lambda db: db.append_message("lineage-2", "assistant", "tip only", finish_reason="stop"))
    later = _list(inbox, changed_since=delta["change_cursor"])
    assert [row["id"] for row in later["sessions"]] == ["lineage-2"]
    assert later["sessions"][0]["last_assistant_reply"]["preview"] == "tip only"
    assert "lineage-2" not in later["tombstones"]


def test_lost_expired_foreign_or_oversized_cursors_answer_repair(inbox, mobile_home, monkeypatch):
    db_path = _ops_db(mobile_home)
    cursor = _list(inbox, change_cursor=1)["change_cursor"]
    for bad in ("garbage", "sc1.0000.0000.1", cursor.rsplit(".", 1)[0] + ".99"):
        repaired = _list(inbox, changed_since=bad)
        assert repaired["repair"] is True and repaired["change_cursor"] is None and repaired["sessions"] == []
    # Foreign scope: the default profile's cursor is not valid for ops.
    default_cursor = _list(inbox, profile="default", change_cursor=1)["change_cursor"]
    assert _list(inbox, changed_since=default_cursor)["repair"] is True

    # Expired: the log no longer reaches back to the cursor.
    monkeypatch.setattr(session_change_cursor, "_RETAIN_ENTRIES", 2)
    for title in ("a", "b", "c"):
        _write(db_path, lambda db, t=title: db.set_session_title("same", t))
        session_change_cursor.refresh(db_path)
    assert _list(inbox, changed_since=cursor)["repair"] is True
    # ...and the full read that follows hands out a working cursor again.
    fresh = _list(inbox, change_cursor=1)["change_cursor"]
    _write(db_path, lambda db: db.set_session_title("same", "d"))
    assert [row["id"] for row in _list(inbox, changed_since=fresh)["sessions"]] == ["same"]

    monkeypatch.setattr(session_change_cursor, "MAX_DELTA_IDS", 1)
    _write(db_path, lambda db: (db.create_session("x1", "desktop"), db.create_session("x2", "desktop")))
    assert _list(inbox, changed_since=fresh)["repair"] is True


def test_replaced_store_changes_the_epoch(inbox, mobile_home):
    db_path = _ops_db(mobile_home)
    cursor = _list(inbox, change_cursor=1)["change_cursor"]
    replacement = db_path.with_name("replacement.db")
    shutil.copy(db_path, replacement)
    os.replace(replacement, db_path)
    _write(db_path, lambda db: db.set_session_title("same", "after replace"))
    assert _list(inbox, changed_since=cursor)["repair"] is True


def test_filters_that_list_untracked_sources_answer_repair(inbox):
    cursor = _list(inbox, change_cursor=1)["change_cursor"]
    for params in ({"exclude_sources": ""}, {"exclude_sources": "cron"}, {"source": "kanban"}):
        assert _list(inbox, changed_since=cursor, **params)["repair"] is True
    assert _list(inbox, changed_since=cursor, source="desktop")["repair"] is False


def test_pending_only_and_foreign_profile_changes_reach_their_scope_only(inbox, mobile_home):
    ops_cursor = _list(inbox, change_cursor=1)["change_cursor"]
    default_cursor = _list(inbox, profile="default", change_cursor=1)["change_cursor"]

    settle = server_requests.send_async("approval", "ops", {"request_id": "r1", "choices": ["once", "deny"]},
                                        lambda result: None)
    ops = _list(inbox, changed_since=ops_cursor)
    assert [row["id"] for row in ops["sessions"]] == ["same"]
    assert ops["sessions"][0]["attention"]["kind"] == "approval"
    default = _list(inbox, profile="default", changed_since=default_cursor)
    assert default["sessions"] == [] and default["tombstones"] == []

    settle("answered")  # the request settling is a change too
    after = _list(inbox, changed_since=ops["change_cursor"])
    assert [row["id"] for row in after["sessions"]] == ["same"] and "attention" not in after["sessions"][0]

    _write(mobile_home / "state.db", lambda db: db.set_session_title("same", "Default only"))
    assert _list(inbox, changed_since=after["change_cursor"])["sessions"] == []
    default = _list(inbox, profile="default", changed_since=default["change_cursor"])
    assert [row["title"] for row in default["sessions"]] == ["Default only"]


# ── RPC session.list changed_since ────────────────────────────────────────────────────────────

def test_rpc_session_list_delta(inbox, mobile_home):
    db_path = _ops_db(mobile_home)
    full = rpc("session.list", profile="ops", change_cursor=True)["result"]
    cursor = full["change_cursor"]
    _write(db_path, lambda db: db.set_session_title("same", "RPC renamed"))
    _write(db_path, lambda db: db.create_session("worker", "kanban"))
    delta = rpc("session.list", profile="ops", changed_since=cursor)["result"]
    assert [(row["id"], row["title"]) for row in delta["sessions"]] == [("same", "RPC renamed")]
    assert delta["repair"] is False and delta["tombstones"] == []
    assert delta["change_cursor"] != cursor
    assert rpc("session.list", profile="ops", changed_since="lost")["result"]["repair"] is True


def test_client_capabilities_opt_in(mobile_home, monkeypatch):
    peer = Peer()
    monkeypatch.setattr(server, "_caller_transport", lambda: peer, raising=False)
    from tui_gateway import methods_voice  # noqa: F401 - handler registered on import
    result = rpc("client.capabilities", server_requests=True, session_change_cursor=True)["result"]
    assert result["session_change_cursor"] == 1
    assert session_change_cursor.is_subscribed(peer)
    rpc("client.capabilities", server_requests=True)
    assert not session_change_cursor.is_subscribed(peer)
    features = rpc("mobile.capabilities")["result"]
    assert "session_change_cursor" in features["features"]
    assert features["feature_versions"]["session_change_cursor"] == 1


# ── journal units ─────────────────────────────────────────────────────────────────────────────

def test_root_hint_follows_compression_edges_only(tmp_path):
    path = tmp_path / "state.db"
    path.write_text("")
    fields = ("id", "source", "parent_session_id", "end_reason", "message_count")
    rows = {"root": ("root", "desktop", None, "compression", 1), "tip": ("tip", "desktop", "root", None, 1),
            "branch": ("branch", "desktop", "tip", None, 1)}

    def observe(values, now):
        return session_change_cursor.observe(
            path, fields, {sid: (repr(v).encode(), v) for sid, v in values.items()}, now=now)
    assert observe(rows, 0.0) is None  # seeds
    rows = {**rows, "tip": ("tip", "desktop", "root", None, 2), "branch": ("branch", "desktop", "tip", None, 2)}
    change = observe(rows, 1.0)
    assert change.changed == {"tip", "branch"}
    sent = []
    session_change_cursor.subscribe(object(), True)
    session_change_cursor.pump(lambda targets, payload: sent.append(payload), now=1.0)
    # tip continues root (root ended with compression); branch's parent did not, so it is its own root.
    assert sent[0]["changed"] == ["branch", "root"]
