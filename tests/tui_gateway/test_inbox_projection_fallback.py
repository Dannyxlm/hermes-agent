"""Unavailable projection evidence must not poison a live Inbox page or flood logs."""
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_state import SessionDB
from tui_gateway import inbox_summaries
from tui_gateway.mobile_push_payloads import Scope
from tui_gateway.mobile_push_store import PushStore


@pytest.mark.parametrize("failure", ["sessions", "runs"])
def test_store_failure_warning_is_bounded_across_runtimes_and_requests(tmp_path, monkeypatch, caplog, failure):
    home = tmp_path / "launch"
    monkeypatch.setenv("HERMES_HOME", str(home))
    entries = []
    for profile in (home, home / "profiles" / "ops"):
        profile.mkdir(parents=True, exist_ok=True)
        with SessionDB(db_path=profile / "state.db") as db:
            db.create_session("stored", "desktop")
        for i in range(25):
            entries.append((f"{profile.name}-{i}", "stored", {"session_key": "stored", "profile_home": str(profile)}))
    push = PushStore(home / "mobile-push" / "outbox.sqlite3", clock=lambda: 123)
    push.close()
    before = [dict(session) for _, _, session in entries]
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(inbox_summaries, "_WARNING_AT", None, raising=False)
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("private error sentinel")
    if failure == "sessions":
        monkeypatch.setattr(SessionDB, "_read_all", unavailable)
    else:
        monkeypatch.setattr(inbox_summaries, "_runs_readonly", unavailable)
    caplog.set_level(logging.WARNING, logger=inbox_summaries.__name__)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: inbox_summaries.live_projections(entries, home), range(8)))
    assert all(len(result) == len(entries) for result in results)
    assert all("latest_run" not in row for result in results for row in result.values())
    warnings = lambda: [r for r in caplog.records if "Inbox run summary unavailable" in r.message]
    assert len(warnings()) == 1
    assert warnings()[0].message == "Inbox run summary unavailable (OperationalError)"
    now[0] += 61
    inbox_summaries.live_projections(entries, home)
    assert len(warnings()) == 2
    assert [session for _, _, session in entries] == before


@pytest.mark.parametrize("invalid", ["cycle", "deep"])
def test_invalid_lineage_does_not_hide_healthy_projection(tmp_path, monkeypatch, caplog, invalid):
    import hermes_state_compression
    monkeypatch.setattr(hermes_state_compression, "_CHAIN_CAP", 4)
    home = tmp_path / "launch"
    monkeypatch.setenv("HERMES_HOME", str(home))
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session("healthy", "desktop")
        db.set_session_title("healthy", "Healthy title")
        db.append_message('healthy', 'assistant', 'Healthy reply', timestamp=100)
        db.create_session('invalid\nscope', 'desktop')
        for i in range(6):
            db.create_session(f"bad-{i}", "desktop", parent_session_id=f"bad-{i-1}" if i else None)
            db._write_sql("UPDATE sessions SET end_reason='compression' WHERE id=?", (f"bad-{i}",))
        if invalid == "cycle":
            db._write_sql("UPDATE sessions SET parent_session_id='bad-5' WHERE id='bad-0'")
    push = PushStore(home / "mobile-push" / "outbox.sqlite3", clock=lambda: 123)
    try:
        push.start_run(Scope("native_session", "default", "healthy"), run_id="healthy-run")
        entries = [(key, key, {"session_key": key}) for key in ("bad-0", "bad-5", "invalid\nscope", "missing", "healthy")]
        caplog.set_level(logging.WARNING, logger=inbox_summaries.__name__)
        for _ in range(3):
            result = inbox_summaries.live_projections(entries, home)
            assert all(result[key] == {} for key in ("bad-0", "bad-5", "invalid\nscope", "missing"))
            assert result["healthy"]["title"] == "Healthy title"
            assert result["healthy"]["last_assistant_reply"]["preview"] == "Healthy reply"
            assert result["healthy"]["latest_run"]["run_id"] == "healthy-run"
        assert not [r for r in caplog.records if "Inbox run summary unavailable" in r.message]
    finally:
        push.close()
