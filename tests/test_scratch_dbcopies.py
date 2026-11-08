"""A finished lane's database copy leaves scratch within the hour, not a day later."""

import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hermes_constants_scratch_dbcopies as dbcopies
from hermes_constants import get_scratch_dir, maybe_prune_scratch_dir, prune_scratch_dir

_OLD = 2 * 3600  # older than the 30 min reclaim window, younger than the 24 h entry prune


@pytest.fixture(autouse=True)
def _small_copies_count(monkeypatch):
    monkeypatch.setattr(dbcopies, "DB_COPY_MIN_BYTES", 1)


def _db(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("create table sessions (id text)")
    conn.close()
    return path


def _age(*paths: Path, seconds: float = _OLD) -> None:
    stamp = time.time() - seconds
    # Deepest first: setting a file's time never moves its parent's, but creating one did.
    for path in sorted(paths, key=lambda p: len(p.parts), reverse=True):
        os.utime(path, (stamp, stamp))


def _lane(scratch: Path, name: str = "d2-cold-listing-tmp") -> tuple[Path, Path, Path]:
    """A finished lane: a state.db copy with a WAL sidecar and a report, last written 2 h ago."""
    entry = scratch / name
    db = _db(entry / "default" / "state.db")
    wal = Path(f"{db}-wal")
    wal.write_bytes(b"wal")
    report = entry / "report.md"
    report.write_text("cold listing p50 41 ms", encoding="utf-8")
    _age(entry, entry / "default", db, wal, report)
    return entry, db, report


def _reclaim(scratch: Path) -> int:
    return dbcopies.reclaim_idle_db_copies(scratch, frozenset(), logging.getLogger(__name__))


def test_finished_lane_loses_its_copy_but_keeps_its_report_and_idle_clock(tmp_path):
    scratch = get_scratch_dir(tmp_path, prune=False)
    entry, db, report = _lane(scratch)
    before = os.lstat(entry).st_mtime_ns, os.lstat(db.parent).st_mtime_ns

    assert prune_scratch_dir(scratch) == 0  # no entry is a day idle: nothing counted as removed

    assert not db.exists() and not Path(f"{db}-wal").exists()
    assert report.read_text(encoding="utf-8") == "cold listing p50 41 ms"
    note = Path(f"{db}.reclaimed").read_text(encoding="utf-8")
    assert "idle SQLite copy" in note and dbcopies.HOLD_NAME in note
    # The 24 h prune still counts from the lane's last write, not from the reclaim.
    assert (os.lstat(entry).st_mtime_ns, os.lstat(db.parent).st_mtime_ns) == before
    log = (tmp_path / "logs" / "scratch-prune.log").read_text(encoding="utf-8")
    assert f"reclaimed idle database copy {str(db)!r}" in log


def test_lane_written_within_the_window_keeps_its_copy(tmp_path):
    scratch = get_scratch_dir(tmp_path, prune=False)
    entry, db, _report = _lane(scratch)
    (entry / "progress.log").write_text("step 3/5", encoding="utf-8")
    assert _reclaim(scratch) == 0
    assert db.exists()


def test_held_snapshot_and_non_copies_are_left_alone(tmp_path):
    scratch = get_scratch_dir(tmp_path, prune=False)
    shared, shared_db, _ = _lane(scratch, "perf-snapshot-train")
    (shared / dbcopies.HOLD_NAME).write_text("speed train D", encoding="utf-8")
    _age(shared, shared / dbcopies.HOLD_NAME)
    other = scratch / "other"
    blob = other / "archive.bin"
    other.mkdir()
    blob.write_bytes(b"not a database" * 10)
    linked = other / "linked.db"
    os.link(_db(tmp_path / "live" / "state.db"), linked)
    _age(other, blob, linked)

    assert _reclaim(scratch) == 0
    assert shared_db.exists() and blob.exists() and linked.exists()


def test_small_database_stays(tmp_path, monkeypatch):
    monkeypatch.setattr(dbcopies, "DB_COPY_MIN_BYTES", 64 * 1024 * 1024)
    scratch = get_scratch_dir(tmp_path, prune=False)
    _entry, db, _ = _lane(scratch)
    assert _reclaim(scratch) == 0
    assert db.exists()


@pytest.mark.platforms("linux")  # open-file and cwd inspection of another process
@pytest.mark.parametrize("how", ["open", "cwd"])
def test_copy_a_live_process_still_uses_is_kept(tmp_path, how):
    scratch = get_scratch_dir(tmp_path, prune=False)
    entry, db, _ = _lane(scratch)
    code = ("import sys, time; fh = open(sys.argv[1], 'rb'); print('ready', flush=True); time.sleep(60)"
            if how == "open" else "import time; print('ready', flush=True); time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", code, str(db)], cwd=str(entry if how == "cwd" else tmp_path),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
        assert _reclaim(scratch) == 0
        assert db.exists()
    finally:
        proc.kill()
        proc.wait(timeout=10)
    assert _reclaim(scratch) > 0  # the lane ended: its copy goes on the next pass
    assert not db.exists()


def test_top_level_copy_file_is_reclaimed(tmp_path):
    scratch = get_scratch_dir(tmp_path, prune=False)
    db = _db(scratch / "state-copy.db")
    _age(db)
    assert _reclaim(scratch) > 0
    assert not db.exists()


def test_prune_is_due_every_quarter_hour_from_any_caller(tmp_path, scratch_prune_spawns):
    scratch = get_scratch_dir(tmp_path, prune=False)
    assert maybe_prune_scratch_dir(scratch) is True
    assert maybe_prune_scratch_dir(scratch) is False  # stamp is fresh
    _age(scratch / ".last_prune", seconds=16 * 60)
    assert maybe_prune_scratch_dir(scratch) is True
    assert scratch_prune_spawns == [scratch, scratch]


def test_gateway_tick_keeps_the_cadence_for_every_served_home(tmp_path, monkeypatch, scratch_prune_spawns):
    from types import SimpleNamespace

    import hermes_constants
    from gateway.run_housekeeping_disk import housekeeping_scratch_prune

    launch, profile = tmp_path / "home", tmp_path / "profiles" / "atlas"
    monkeypatch.setattr(hermes_constants, "get_routing_process_hermes_home", lambda: launch)
    runner = SimpleNamespace(_served_profile_homes={"atlas": profile})
    housekeeping_scratch_prune(runner)
    housekeeping_scratch_prune(runner)  # within the interval: no second pass
    assert sorted(scratch_prune_spawns) == sorted([launch / "cache" / "scratch", profile / "cache" / "scratch"])
