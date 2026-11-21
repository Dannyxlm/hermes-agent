"""Family launch metadata under write contention (v0.23.2 merge).

Launch writes are best effort: they never retry behind another process's SQLite write lock. They
must still queue behind this process's own writer mutex: upstream 4c5b875f69 made ``patience_s``
bound the mutex wait too, and with the launch writers' ``patience_s=0`` every launch record was
dropped whenever another thread of the gateway was mid-write.
"""
import sqlite3
import threading
import time

import pytest

from tests.persistence_sqlite.core._fork_store import SessionDB
from persistence.sqlite._state.write import WriterMutexTimeout


@pytest.fixture()
def db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", "cli", profile_name="default")
    db.record_launch("L1", kind="helper", launcher_profile="default", launcher_session_id="parent",
                     child_profile="default")
    yield db
    db.close()


def _finished(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT terminal_cause, finished_at IS NOT NULL FROM work_launches WHERE launch_id='L1'").fetchone()


def _hold_writer_mutex(db, seconds):
    held, done = threading.Event(), threading.Event()

    def hold():
        with db._lock:
            held.set()
            time.sleep(seconds)
        done.set()

    threading.Thread(target=hold, daemon=True).start()
    assert held.wait(5)
    return done


def test_launch_write_queues_behind_an_in_process_writer(db, tmp_path):
    done = _hold_writer_mutex(db, 0.3)  # a transcript flush or a sibling's finish on another thread
    db.finish_launch("L1", "success")
    assert done.is_set()
    assert _finished(tmp_path / "state.db") == ("success", 1)


def test_launch_write_mutex_wait_stays_bounded(db, tmp_path):
    db._WRITE_PATIENCE_S = 0.2  # the mutex budget launch writes use
    done = _hold_writer_mutex(db, 1.5)
    started = time.monotonic()
    with pytest.raises(WriterMutexTimeout):
        db.finish_launch("L1", "success")
    assert time.monotonic() - started < 1.2
    assert done.wait(5)
    assert _finished(tmp_path / "state.db") == (None, 0)


def test_launch_write_still_skips_foreign_busy_retries(db, tmp_path):
    foreign = sqlite3.connect(tmp_path / "state.db", timeout=0)
    try:
        foreign.execute("BEGIN IMMEDIATE")  # another Hermes process holds the SQLite write lock
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            db.finish_launch("L1", "success")
        # Refused at the connection's own busy timeout, never the 20 s routine write patience.
        assert time.monotonic() - started < db._WRITE_PATIENCE_S / 2
    finally:
        foreign.rollback()
        foreign.close()
