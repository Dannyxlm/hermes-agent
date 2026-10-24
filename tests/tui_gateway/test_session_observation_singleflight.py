"""Concurrent watcher/push/list observations share one settled store read."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

from tui_gateway import server


def test_concurrent_observers_share_one_store_scan(tmp_path, monkeypatch):
    import hermes_state
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, source TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('one', 'Original', 'desktop')")
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    monkeypatch.setattr(server, "_settled_mtime", lambda stamp: stamp)
    first_read = threading.Event()
    duplicate_read = threading.Event()
    second_started = threading.Event()
    opened = []
    connect = hermes_state._connect_tracked_db

    def track(*args, **kwargs):
        opened.append(args[0])
        if len(opened) == 1:
            first_read.set()
            assert second_started.wait(5)
            duplicate_read.wait(2)
        else:
            duplicate_read.set()
        return connect(*args, **kwargs)

    def second():
        second_started.set()
        return server._session_db_content_sig(path)

    monkeypatch.setattr(hermes_state, "_connect_tracked_db", track)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(server._session_db_content_sig, path)
        assert first_read.wait(5)
        b = pool.submit(second)
        assert a.result(timeout=10) == b.result(timeout=10)
    assert len(opened) == 1, "Overlapping observers must share the same settled WAL observation"
