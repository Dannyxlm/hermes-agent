"""The SQLite tripwire also covers URI paths and interpreter-path exemptions."""
import sqlite3

import pytest

from tests.home_io_guard import HomeIOGuard


@pytest.mark.parametrize("uri", [False, True])
def test_shared_kanban_is_refused_before_sqlite_open(tmp_path, monkeypatch, uri):
    shared = tmp_path / "shared"
    shared.mkdir()
    target = shared / "kanban.db"
    from tests import home_io_guard

    monkeypatch.setattr(home_io_guard, "_INTERPRETER_PREFIX_STRS", (str(shared),))
    HomeIOGuard(lambda: (), kanban_roots=lambda: (shared,)).install(monkeypatch)
    database = target.as_uri() + "?mode=rwc" if uri else target
    with pytest.raises(AssertionError, match="set HERMES_KANBAN_HOME to isolate Kanban"):
        sqlite3.connect(database, uri=uri)
    assert not target.exists()
    safe = tmp_path / "isolated.db"
    with sqlite3.connect(safe) as conn:
        conn.execute("CREATE TABLE proof (value TEXT)")
        conn.execute("INSERT INTO proof VALUES ('isolated')")
        assert conn.execute("SELECT value FROM proof").fetchone() == ("isolated",)
