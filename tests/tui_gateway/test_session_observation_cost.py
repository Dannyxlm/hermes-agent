"""How much work the session change journal costs, and what it must never skip.

The watcher and every list read observe a store by reading all of its ``sessions`` rows. Counted
through the real entrypoints (``_session_db_content_sig``, ``GET /api/sessions``) by the ``sessions``
scans SQLite executes: no timing.
"""

import os
import sqlite3
import threading

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_inbox_summaries import inbox  # noqa: F401  (fixture)
from tests.tui_gateway.test_methods_mobile import mobile_home  # noqa: F401  (fixture)
from tests.tui_gateway.test_session_change_cursor import PHONE_EXCLUDES
from tui_gateway import server, session_change_cursor


class FlakyConnection(sqlite3.Connection):
    """Fails the next statement containing ``fail_on``, as a busy or locked store does."""

    fail_on = None

    def execute(self, sql, *args):
        if FlakyConnection.fail_on and FlakyConnection.fail_on in sql:
            FlakyConnection.fail_on = None
            raise sqlite3.OperationalError("database is locked")
        return super().execute(sql, *args)


@pytest.fixture
def flaky(monkeypatch):
    import hermes_state
    real = hermes_state._connect_tracked_db
    monkeypatch.setattr(hermes_state, "_connect_tracked_db",
                        lambda *args, **kwargs: real(*args, **{**kwargs, "factory": FlakyConnection}))
    FlakyConnection.fail_on = None
    yield FlakyConnection
    FlakyConnection.fail_on = None


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
    """The test thread plays the watcher: an observation it makes is the watcher's own."""
    monkeypatch.setattr(server, "_change_watcher_thread", threading.current_thread(), raising=False)


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
    """A guard: green on base too, where every request observes inline."""
    db_path = mobile_home / "profiles" / "ops" / "state.db"
    cursor = _list(inbox, change_cursor=1)["change_cursor"]
    server._session_db_content_sig(db_path)
    server._session_observed_at[os.path.realpath(db_path)] -= 1.0  # alive, but silent for a second
    with SessionDB(db_path=db_path) as db:
        db.set_session_title("same", "Renamed")

    assert [row["id"] for row in _list(inbox, changed_since=cursor)["sessions"]] == ["same"]
    assert scans


def test_list_request_observes_inline_when_the_watcher_thread_is_dead(inbox, mobile_home, scans, live_watcher, monkeypatch):  # noqa: F811
    db_path = mobile_home / "profiles" / "ops" / "state.db"
    cursor = _list(inbox, change_cursor=1)["change_cursor"]
    server._session_db_content_sig(db_path)  # the watcher's pass...
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    monkeypatch.setattr(server, "_change_watcher_thread", dead, raising=False)  # ...then nobody is watching any more
    with SessionDB(db_path=db_path) as db:
        db.set_session_title("same", "Renamed")

    assert [row["id"] for row in _list(inbox, changed_since=cursor)["sessions"]] == ["same"]
    assert scans


def test_an_inline_read_never_vouches_for_the_next_one(inbox, mobile_home, scans, live_watcher):  # noqa: F811
    """A store the watcher does not visit (a profile outside its set): only the watcher's own pass may
    make a request skip its observation, never another request's."""
    db_path = mobile_home / "profiles" / "ops" / "state.db"
    cursor = _list(inbox, change_cursor=1)["change_cursor"]  # an inline observation, not the watcher's
    with SessionDB(db_path=db_path) as db:
        db.set_session_title("same", "Renamed")

    assert [row["id"] for row in _list(inbox, changed_since=cursor)["sessions"]] == ["same"]


def test_a_reopened_probe_never_serves_a_stale_version(tmp_path, flaky):
    """A new connection's ``data_version`` restarts, so a version cached on the old one must not match."""
    path = _store(tmp_path)
    server._session_db_content_sig(path)  # seeds: cached under the first connection's version
    cursor = session_change_cursor.current_cursor(path)
    before = server._session_db_content_sig(path)
    with SessionDB(db_path=path) as db:
        db.set_session_title("a", "Renamed")

    flaky.fail_on = "FROM sessions ORDER BY id"  # the row read fails and the probe is dropped
    assert server._session_db_content_sig(path) == before  # a failed read keeps the last digest
    assert server._session_db_content_sig(path) != before  # the reopened probe must read the rename
    touched, _ = session_change_cursor.changes_since(path, cursor)
    assert touched == {"a"}


def test_a_failing_version_probe_still_reads_the_rows_that_pass(tmp_path, flaky):
    path = _store(tmp_path)
    server._session_db_content_sig(path)
    before = server._session_db_content_sig(path)
    with SessionDB(db_path=path) as db:
        db.set_session_title("a", "Renamed")

    flaky.fail_on = "PRAGMA data_version"
    assert server._session_db_content_sig(path) != before


def _release_for_raw_access(path):
    """What a repair does before it raw-copies a store: let go of what can be let go, then ask."""
    from hermes_cli.sqlite_safe_read import has_live_connection, release_evictable
    release_evictable(path)
    return has_live_connection(path)


def test_the_watchers_probe_does_not_stop_a_repair_copy(tmp_path, live_watcher):
    """``_backup_db_file`` refuses while this process holds a connection to the store, and a refused backup
    is a hard stop for repair. The watcher's idle probe is closed on demand instead of counting."""
    from hermes_state_repair import _backup_db_file
    path = _store(tmp_path)
    server._session_db_content_sig(path)

    backup, reason = _backup_db_file(path)

    assert backup is not None, reason


def test_another_live_connection_still_stops_a_repair_copy(tmp_path, live_watcher):
    from hermes_state_repair import _backup_db_file
    path = _store(tmp_path)
    server._session_db_content_sig(path)
    with SessionDB(db_path=path):
        backup, reason = _backup_db_file(path)
    assert backup is None and "still open" in reason


def test_changes_still_surface_while_the_probe_is_held_off(tmp_path, live_watcher):
    path = _store(tmp_path)
    server._session_db_content_sig(path)
    assert not _release_for_raw_access(path)
    cursor = session_change_cursor.current_cursor(path)
    with SessionDB(db_path=path) as db:
        db.set_session_title("b", "Renamed")

    server._session_db_content_sig(path)
    touched, _ = session_change_cursor.changes_since(path, cursor)
    assert touched == {"b"}
    assert not _tracked_by_this_process(path)  # held off: no kept probe for a while


def test_the_probe_comes_back_when_the_hold_off_ends(tmp_path, live_watcher, monkeypatch, scans):
    monkeypatch.setattr(server, "_PROBE_HOLDOFF_S", 0.0, raising=False)
    path = _store(tmp_path)
    server._session_db_content_sig(path)
    assert not _release_for_raw_access(path)
    server._session_db_content_sig(path)  # reopens and reads once
    scans.clear()
    server._session_db_content_sig(path)
    assert scans == []  # a quiet store is a version hit again, not a re-read


def _tracked_by_this_process(path):
    from hermes_cli.sqlite_safe_read import has_live_connection
    return has_live_connection(path)
