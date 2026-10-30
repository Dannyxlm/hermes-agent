"""The conversation-lineage index of a list request parses receipts only when they changed.

A store holds one ~43 KB creation receipt per local conversation; finding the few that carry a reset or
compression lineage means parsing every one of them, and every ``GET /api/sessions`` did, then found
nothing. Counted by the parse statements SQLite runs behind the real route handler: no timing.
"""
import json

import pytest

import hermes_state
import hermes_state_local as local
from hermes_cli.web_routers import sessions
from hermes_state import SessionDB


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(local, "_LINEAGE_RECEIPTS", {}, raising=False)


@pytest.fixture
def parses(monkeypatch):
    """Every statement that reads receipt values to look inside their lineage."""
    real = hermes_state._connect_tracked_db
    seen = []

    def tracked(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_trace_callback(lambda sql: seen.append(sql) if "json_array_length" in sql else None)
        return conn

    monkeypatch.setattr(hermes_state, "_connect_tracked_db", tracked)
    return seen


def seed(path, receipts=200):
    with SessionDB(db_path=path) as db:
        db.create_session("owner", "desktop")
        db.create_session("solo", "desktop")
        for i in range(receipts):
            db._write_sql("INSERT INTO state_meta(key,value) VALUES(?,?)", (
                local.POLICY_PREFIX + f"filler-{i}",
                json.dumps({"session_id": f"filler-{i}", "lineage": [f"filler-{i}"], "policy": "x" * 2000})))
        reset(db, "owner", "next", ["owner", "next"])
    return path


def reset(db, previous, new, lineage, *, grow_in_place=False):
    db.end_session(previous, end_reason="session_reset")
    db.create_session(new, "desktop", parent_session_id=previous, model_config={"_reset_from": previous})
    value = json.dumps({"session_id": "owner", "lineage": lineage})
    if grow_in_place:  # what a canonical reset does: UPDATE the receipt row, keeping its rowid
        db._write_sql("UPDATE state_meta SET value=? WHERE key=?", (value, local.POLICY_PREFIX + "owner"))
    else:
        db._write_sql("INSERT OR REPLACE INTO state_meta(key,value) VALUES(?,?)", (local.POLICY_PREFIX + "owner", value))


@pytest.fixture
def list_ids(monkeypatch, tmp_path):
    path = seed(tmp_path / "state.db")
    monkeypatch.setattr(sessions, "_open_session_db_for_profile",
                        lambda profile, read_only: SessionDB(db_path=path, read_only=True))
    monkeypatch.setattr(sessions, "_cron_profile_home", lambda profile: ("ops", tmp_path))

    def ids():
        result = sessions.get_sessions(
            limit=100, offset=0, min_messages=0, archived="exclude", order="recent",
            source=None, sources=None, exclude_sources=None, cwd_prefix=None,
            full=False, profile="ops", changed_since=None, change_cursor=False)
        return sorted(row["id"] for row in result["sessions"])

    ids.path = path
    return ids


def test_requests_between_unrelated_commits_parse_the_receipts_once(list_ids, parses):
    assert list_ids() == ["next", "solo"]
    with SessionDB(db_path=list_ids.path) as db:
        db.set_session_title("solo", "Renamed")  # a commit, but not a receipt write
    assert list_ids() == ["next", "solo"]
    assert len(parses) <= 1


@pytest.mark.parametrize("grow_in_place", [False, True], ids=["replaced", "updated-in-place"])
def test_a_reset_after_the_first_request_is_listed_by_the_next(list_ids, grow_in_place):
    assert list_ids() == ["next", "solo"]
    with SessionDB(db_path=list_ids.path) as db:
        reset(db, "next", "newest", ["owner", "next", "newest"], grow_in_place=grow_in_place)
    assert list_ids() == ["newest", "solo"]


def test_a_deleted_receipt_is_not_served_from_the_cache(list_ids):
    assert list_ids() == ["next", "solo"]
    with SessionDB(db_path=list_ids.path) as db:
        db._write_sql("DELETE FROM state_meta WHERE key=?", (local.POLICY_PREFIX + "owner",))
    assert list_ids() == ["next", "owner", "solo"]


def test_segment_end_reasons_are_read_fresh_even_when_no_receipt_changed(list_ids):
    """Only the receipts are cached; whether a segment ended in a reset is read from its row each time."""
    assert list_ids() == ["next", "solo"]
    with SessionDB(db_path=list_ids.path) as db:
        db._write_sql("UPDATE sessions SET end_reason='compression' WHERE id='owner'")
    assert "owner" in list_ids()
