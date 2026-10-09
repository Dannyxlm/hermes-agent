"""Selector-only rollback compatibility of the persistent push store.

The live predecessor (hxr8, ``031aed80ce67``) inserts runs positionally with 8 values,
so this store must never widen ``runs``: run ownership, close reason and presentation
live in the ``run_state`` side table. The predecessor is loaded from git at that exact
commit (never ``HEAD``) and driven against the same SQLite file as the current store.
"""

import importlib
import importlib.util
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tui_gateway.mobile_push_payloads import Scope
from tui_gateway.mobile_push_provider import DeliveryResult
from tui_gateway.mobile_push_store import PushStore, process_owner

PREDECESSOR = "031aed80ce67"
_MODULES = ("mobile_push_payloads", "mobile_push_widgets", "mobile_push_store")
_RUN_COLUMNS = ["run_id", "scope", "surface", "profile", "session_id", "status", "started_at", "updated_at"]
DAY = 86400


@pytest.fixture
def old(tmp_path):
    """The predecessor's push store package, loaded under a private package name."""
    repo = Path(__file__).resolve().parents[2]
    package = f"_hxr8_push_{uuid.uuid4().hex}"
    root = tmp_path / package
    root.mkdir()
    (root / "__init__.py").write_text("")
    for name in _MODULES:
        shown = subprocess.run(["git", "-C", str(repo), "show", f"{PREDECESSOR}:tui_gateway/{name}.py"],
                               capture_output=True, text=True)
        if shown.returncode:
            pytest.skip(f"predecessor {PREDECESSOR} unavailable in this checkout")
        (root / f"{name}.py").write_text(shown.stdout)
    spec = importlib.util.spec_from_file_location(package, root / "__init__.py",
                                                  submodule_search_locations=[str(root)])
    sys.modules[package] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sys.modules[package])
    try:
        payloads = importlib.import_module(f"{package}.mobile_push_payloads")
        store = importlib.import_module(f"{package}.mobile_push_store")
        yield store.PushStore, payloads.Scope
    finally:
        for key in [k for k in sys.modules if k == package or k.startswith(package + ".")]:
            del sys.modules[key]


def ids():
    return {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}


def runs_columns(store):
    return [row[1] for row in store._db.execute("PRAGMA table_info(runs)")]


def run_state_count(store):
    return store._db.execute("SELECT COUNT(*) FROM run_state").fetchone()[0]


def exercise_predecessor(OldStore, OldScope, path, now, session):
    """Everything hxr8 does with a store: it must all keep working after hxr9 touched the file."""
    store = OldStore(path, lambda: now[0])
    try:
        scope = OldScope("native_session", "ops", session)
        device = ids()
        store.register("principal", scope, **device, token="ab" * 32, environment="production")
        run = store.start_run(scope)
        assert run["status"] == "starting"
        now[0] += 5
        store.presence("principal", scope, **device, foreground=True)
        assert store.record(scope, run["run_id"], "question", "waitingForApproval") is True
        store.presence("principal", scope, **device, foreground=False)
        now[0] += 61  # the foreground lease lapses; the terminal alert must reach the outbox
        assert store.record(scope, run["run_id"], "done", "complete") is True
        job = store.claim()
        assert job is not None and job["kind"] == "alert" and job["run_id"] == run["run_id"]
        store.finish(job, DeliveryResult("accepted"))
        assert store.claim() is None
        assert store.current_run(scope)["status"] == "complete"
        assert runs_columns(store) == _RUN_COLUMNS
        assert store._db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        return run
    finally:
        store.close()


def test_predecessor_keeps_working_after_a_round_trip_through_the_current_store(tmp_path, old):
    OldStore, OldScope = old
    path = tmp_path / "push.sqlite"
    now = [1800000000.0]
    exercise_predecessor(OldStore, OldScope, path, now, "before")

    current = PushStore(path, lambda: now[0])
    try:
        scope = Scope("native_session", "ops", "candidate")
        current.register("principal", scope, **ids(), token="cd" * 32, environment="production")
        run = current.start_run(scope)
        assert run["owner"] == current.owner
        current.record(scope, run["run_id"], "step", "thinking")
        current.start_run(scope)  # supersedes the first run: writes a close reason
        assert current.current_run(scope)["owner"] == current.owner
    finally:
        current.close()

    exercise_predecessor(OldStore, OldScope, path, now, "after")  # hxr9 never widened the hxr8 table

    # The predecessor's 30-day prune runs on a file holding hxr9 side rows.
    now[0] += 31 * DAY
    store = OldStore(path, lambda: now[0])
    try:
        store.maintain()
        assert store._db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        store.close()

    # Rolling forward again removes the side rows whose runs the predecessor pruned.
    current = PushStore(path, lambda: now[0])
    try:
        assert run_state_count(current) == 2  # both hxr9 runs left side rows behind
        current.maintain()
        assert run_state_count(current) == 0
        assert current._db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        current.close()


def test_runs_made_by_the_predecessor_during_a_rollback_read_back_as_legacy(tmp_path, old):
    OldStore, OldScope = old
    path = tmp_path / "push.sqlite"
    now = [1800000000.0]
    current = PushStore(path, lambda: now[0])
    mine = Scope("native_session", "ops", "candidate")
    preview_run = current.start_run(mine)
    current.close()

    legacy = exercise_predecessor(OldStore, OldScope, path, now, "rollback")
    open_legacy = OldStore(path, lambda: now[0])
    try:
        stuck = open_legacy.start_run(OldScope("native_session", "ops", "stuck"))
    finally:
        open_legacy.close()

    now[0] += 700
    current = PushStore(path, lambda: now[0])
    try:
        assert runs_columns(current) == _RUN_COLUMNS
        read = current.current_run(Scope("native_session", "ops", "rollback"))
        assert read["run_id"] == legacy["run_id"]
        assert (read["owner"], read["closed_reason"], read["presentation"]) == ("", "", "")
        assert current.current_run(mine)["owner"] == process_owner()
        assert current.current_run(mine)["run_id"] == preview_run["run_id"]
        # A legacy open run has no owner, so the first sweep closes it silently.
        assert current.reconcile_orphans(set(), set()) == 2
        closed = current.current_run(Scope("native_session", "ops", "stuck"))
        assert (closed["run_id"], closed["status"], closed["closed_reason"]) == (stuck["run_id"], "cancelled", "orphaned")
        assert current.current_run(mine)["closed_reason"] == "orphaned"  # unheld past the idle grace
        restarted = current.start_run(Scope("native_session", "ops", "stuck"))
        assert restarted["owner"] == current.owner and restarted["closed_reason"] == ""
    finally:
        current.close()


def test_a_store_already_widened_by_the_first_hxr9_build_keeps_its_run_state(tmp_path):
    """Dev stores migrated by the ALTER build still read; their values move to the side table."""
    path = tmp_path / "push.sqlite"
    now = [1800000000.0]
    scope = Scope("native_session", "ops", "w")
    PushStore(path, lambda: now[0]).close()
    db = sqlite3.connect(path)
    widened = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
    for column in ("owner", "closed_reason", "presentation"):
        if column not in widened:  # the ALTER build had already added them itself
            db.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    db.execute("""INSERT INTO runs VALUES ('widened',?,'native_session','ops','w',
        'starting',?,?,'4242:7','','{"v":1}')""", (scope.key, now[0], now[0]))
    db.commit()
    db.close()
    current = PushStore(path, lambda: now[0])
    try:
        read = current.current_run(scope)
        assert (read["owner"], read["presentation"]) == ("4242:7", '{"v":1}')
        assert current.start_run(scope)["owner"] == current.owner
        assert current.current_run(scope)["owner"] == current.owner
        assert len(runs_columns(current)) == 11  # never rebuilt or narrowed here
    finally:
        current.close()
