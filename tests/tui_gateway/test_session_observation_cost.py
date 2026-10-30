"""How much work the session change journal costs, and what it must never skip.

The watcher and every list read observe a store by reading all of its ``sessions`` rows. Counted
through the real entrypoints (``_session_db_content_sig``, ``GET /api/sessions``) by the ``sessions``
scans SQLite executes: no timing.
"""

import sqlite3
import threading

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_inbox_summaries import inbox  # noqa: F401  (fixture)
from tests.tui_gateway.test_methods_mobile import mobile_home  # noqa: F401  (fixture)
from tests.tui_gateway.test_session_change_cursor import PHONE_EXCLUDES
from tui_gateway import server, session_change_cursor


@pytest.fixture(autouse=True)
def clean_journal(monkeypatch):
    session_change_cursor.reset_for_tests()
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    monkeypatch.setattr(server, "_session_observed_at", {}, raising=False)
    yield
    session_change_cursor.reset_for_tests()


@pytest.fixture
def scans(monkeypatch):
    """Every full ``sessions`` read any watcher/list connection runs."""
    import hermes_state
    real = hermes_state._connect_tracked_db
    seen = []

    def tracked(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_trace_callback(lambda sql: seen.append(sql) if "FROM sessions ORDER BY id" in sql else None)
        return conn

    monkeypatch.setattr(hermes_state, "_connect_tracked_db", tracked)
    return seen


@pytest.fixture
def live_watcher(monkeypatch):
    stop = threading.Event()
    thread = threading.Thread(target=stop.wait, daemon=True)
    thread.start()
    monkeypatch.setattr(server, "_change_watcher_thread", thread, raising=False)
    yield thread
    stop.set()
    thread.join(5)


def _store(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as db:
        db.create_session("a", "desktop")
        db.create_session("b", "desktop")
    return path


def test_a_quiet_store_is_read_once_even_right_after_its_last_commit(tmp_path, scans):
    path = _store(tmp_path)
    for _ in range(3):
        server._session_db_content_sig(path)
    assert len(scans) == 1


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda db: db.create_session("c", "desktop"), id="insert"),
    pytest.param(lambda db: db.set_session_title("a", "Renamed"), id="rename"),
    pytest.param(lambda db: db.set_session_archived("a", True), id="archive"),
    pytest.param(lambda db: db._conn.execute("UPDATE sessions SET last_read_at=123.0 WHERE id='a'"), id="read-marker"),
    pytest.param(lambda db: db.delete_session("b"), id="delete"),
])
def test_every_kind_of_session_change_surfaces_in_the_very_next_observation(tmp_path, mutate):
    path = _store(tmp_path)
    server._session_db_content_sig(path)  # seeds the journal
    cursor = session_change_cursor.current_cursor(path)
    before = server._session_db_content_sig(path)

    with SessionDB(db_path=path) as db:  # a commit in the same file-timestamp tick as the read above
        mutate(db)
        db._conn.commit()

    assert server._session_db_content_sig(path) != before
    touched, _ = session_change_cursor.changes_since(path, cursor)
    assert touched and touched <= {"a", "b", "c"}


def test_a_replaced_store_is_read_from_the_new_file(tmp_path, scans):
    path = _store(tmp_path)
    server._session_db_content_sig(path)
    other = tmp_path / "other.db"
    with SessionDB(db_path=other) as db:
        db.create_session("only-in-new-file", "desktop")
    other.replace(path)
    server._session_db_content_sig(path)
    touched, _ = session_change_cursor.changes_since(path, session_change_cursor.current_cursor(path))
    assert touched == set() and len(scans) == 2
    with sqlite3.connect(path) as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM sessions")] == ["only-in-new-file"]


def _list(client, **params):
    response = client.get("/api/sessions", params={
        "profile": "ops", "order": "recent", "exclude_sources": PHONE_EXCLUDES, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_list_request_trusts_a_live_watcher_for_250ms_and_loses_nothing(inbox, mobile_home, scans, live_watcher):  # noqa: F811
    db_path = mobile_home / "profiles" / "ops" / "state.db"
    cursor = _list(inbox, change_cursor=1)["change_cursor"]
    server._session_db_content_sig(db_path)  # the watcher's own pass: the store is now vouched for
    with SessionDB(db_path=db_path) as db:
        db.set_session_title("same", "Renamed")
    scans.clear()

    assert _list(inbox, changed_since=cursor)["sessions"] == []
    assert scans == []  # the request did not rescan every session

    server._session_db_content_sig(db_path)  # the watcher's next pass
    assert [row["id"] for row in _list(inbox, changed_since=cursor)["sessions"]] == ["same"]


def test_list_request_observes_inline_when_the_watcher_stalled(inbox, mobile_home, scans, live_watcher):  # noqa: F811
    db_path = mobile_home / "profiles" / "ops" / "state.db"
    cursor = _list(inbox, change_cursor=1)["change_cursor"]
    server._session_db_content_sig(db_path)
    server._session_observed_at[str(db_path.resolve())] -= 1.0  # alive, but silent for a second
    with SessionDB(db_path=db_path) as db:
        db.set_session_title("same", "Renamed")

    assert [row["id"] for row in _list(inbox, changed_since=cursor)["sessions"]] == ["same"]
    assert scans


def test_list_request_observes_inline_when_the_watcher_thread_is_dead(inbox, mobile_home, scans, monkeypatch):  # noqa: F811
    db_path = mobile_home / "profiles" / "ops" / "state.db"
    cursor = _list(inbox, change_cursor=1)["change_cursor"]
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    monkeypatch.setattr(server, "_change_watcher_thread", dead, raising=False)
    server._session_db_content_sig(db_path)  # observed a moment ago, but nobody is watching any more
    with SessionDB(db_path=db_path) as db:
        db.set_session_title("same", "Renamed")

    assert [row["id"] for row in _list(inbox, changed_since=cursor)["sessions"]] == ["same"]
    assert scans
