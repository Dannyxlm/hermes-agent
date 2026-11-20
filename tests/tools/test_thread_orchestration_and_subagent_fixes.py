"""Regression tests for thread orchestration performance, finite-turn background safety,
uninitialized profile-store resilience, and subagent output_schema failure guards."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_routed_banner_memo_caches_per_profile_home(tmp_path):
    """Under HERMES_HOME override (multiplexed gateway owner_scope), _memo caches per
    resolved home instead of rescanning all skills on every session.resume."""
    from hermes_cli import banner
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home_a = tmp_path / "profile_a"
    home_b = tmp_path / "profile_b"
    home_a.mkdir()
    home_b.mkdir()
    banner._routed_memo_cache.clear()
    calls: list[str] = []

    def fake_find():
        from hermes_constants import get_hermes_home
        h = get_hermes_home().name
        calls.append(h)
        return [{"name": f"skill-{h}", "category": "ops"}]

    try:
        with patch("tools.skills_tool.find_all_skills", side_effect=fake_find):
            tok = set_hermes_home_override(str(home_a))
            try:
                first_a = banner.get_available_skills()
                second_a = banner.get_available_skills()
            finally:
                reset_hermes_home_override(tok)

            tok = set_hermes_home_override(str(home_b))
            try:
                first_b = banner.get_available_skills()
                second_b = banner.get_available_skills()
            finally:
                reset_hermes_home_override(tok)
    finally:
        banner._routed_memo_cache.clear()

    assert first_a == second_a == {"ops": ["skill-profile_a"]}
    assert first_b == second_b == {"ops": ["skill-profile_b"]}
    assert calls == ["profile_a", "profile_b"]


def test_cli_skills_caches_per_home(tmp_path):
    """_cli_skills caches its kanban-scoped grouped skills per home instead of popping
    _SKILLS_CACHE and rescanning disk on every CLI resume."""
    from gateway import session_local_inventory as inv
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = tmp_path / "cli_home"
    home.mkdir()
    inv._CLI_SKILLS_CACHE.clear()
    calls = 0

    def fake_find():
        nonlocal calls
        calls += 1
        return [{"name": "kanban-helper", "category": "devops"}]

    tok = set_hermes_home_override(str(home))
    try:
        with patch("tools.skills_tool.find_all_skills", side_effect=fake_find):
            r1 = inv._cli_skills()
            r2 = inv._cli_skills()
    finally:
        reset_hermes_home_override(tok)
        inv._CLI_SKILLS_CACHE.clear()

    assert r1 == r2 == {"devops": ["kanban-helper"]}
    assert calls == 1


def test_finite_turn_disables_background_notify_on_complete():
    """A finite=True turn (native thread, one-shot) must not arm post-turn notify_on_complete
    even when the underlying local gateway supports async delivery."""
    from gateway.session_context import clear_session_vars, set_session_vars
    from gateway.session_finite import finite_turn_scope
    from tools.terminal_tool_background import _apply_async_support

    proc = SimpleNamespace(id="proc_finite_test", watcher_platform="")
    tokens = set_session_vars(
        platform="local", chat_id="local-1", session_key="local-1", async_delivery=True
    )
    try:
        with finite_turn_scope(True):
            result_data: dict = {"status": "running", "notify_on_complete": True}
            notify, watch = _apply_async_support(proc, result_data, True, ["READY"])
    finally:
        clear_session_vars(tokens)

    assert notify is False
    assert watch is None
    assert result_data["notify_on_complete"] is False
    assert "poll" in result_data.get("notify_unsupported", "").lower()
    assert proc.watcher_platform == ""


def test_thread_wake_skips_uninitialized_profile_store_without_work_launches(tmp_path, monkeypatch):
    """An uninitialized/empty profile state.db missing work_launches must not crash
    thread_wake.leads() or thread_state.threads_of() for healthy profiles."""
    from gateway import thread_wake
    from tools import thread_state as ts

    good_home = tmp_path / "good"
    bad_home = tmp_path / "bad"
    good_home.mkdir()
    bad_home.mkdir()

    # bad_home has a state.db with no work_launches table
    bad_conn = sqlite3.connect(bad_home / "state.db")
    bad_conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    bad_conn.commit()
    bad_conn.close()

    # good_home has a minimal valid sessions + work_launches schema with one active thread
    good_conn = sqlite3.connect(good_home / "state.db")
    good_conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            parent_session_id TEXT,
            end_reason TEXT,
            started_at REAL,
            last_activity_at REAL
        );
        CREATE TABLE work_launches (
            launch_id TEXT PRIMARY KEY,
            kind TEXT,
            child_profile TEXT,
            child_session_id TEXT,
            launcher_profile TEXT,
            launcher_session_id TEXT,
            title TEXT,
            created_at REAL
        );
        """
    )
    now = 1_800_000_000.0
    good_conn.execute(
        "INSERT INTO sessions VALUES (?, NULL, NULL, ?, ?)",
        ("child-1", now - 60, now - 10),
    )
    good_conn.execute(
        "INSERT INTO work_launches VALUES (?, 'thread', 'default', 'child-1', 'default', 'lead-1', 'T1', ?)",
        ("thread:abc", now - 60),
    )
    good_conn.commit()
    good_conn.close()

    monkeypatch.setattr(ts, "homes", lambda: {"default": good_home, "broken": bad_home})
    assert thread_wake.leads("default", now=now) == ["lead-1"]
    found = ts.threads_of("default", {"lead-1"}, since=now - 3600)
    assert [row["child_session_id"] for row in found] == ["child-1"]


def test_output_schema_does_not_retry_failed_api_turn():
    """When a subagent's first turn failed (e.g. HTTP 429 error text in final_response),
    _validate_child_output_schema must not fire a schema-correction retry turn."""
    from tools.delegate_tool_child_run import _validate_child_output_schema

    child = MagicMock()
    child._delegate_output_schema = {"type": "object", "required": ["verdict"]}
    failed_result = {
        "completed": False,
        "failed": True,
        "error": "Anthropic rate-limited every one of 3 attempts",
        "final_response": "Anthropic rate-limited every one of 3 attempts — temporarily unavailable.",
        "api_calls": 3,
    }

    outcome = _validate_child_output_schema(child, failed_result, 0, "child-0", None)
    assert outcome.retries == 0
    assert outcome.valid is False
    child.run_conversation.assert_not_called()


def test_canonical_resume_omit_info_skips_snapshot_info_and_hooks():
    """Canonical session.resume with omit_info=True skips snapshot_info and refresh_profile_hooks
    while preserving session_id and revision for thread_wake / thread_tool."""
    from gateway.config import Platform
    from gateway.session_contract import SessionRef
    from gateway.session_controls import AuthorityConnection

    ref = SessionRef("/tmp/fake-home", "sess-1")
    snap = SimpleNamespace(
        handle=SimpleNamespace(ref=ref, execution_state="idle", authority_epoch=1, revision=7, execution_generation=1),
        subscription_id="sub-1",
        replay_epoch=1,
        last_sequence=0,
        history=[],
        pending=[],
        prompts=[],
    )
    live = SimpleNamespace(
        source=SimpleNamespace(platform=Platform.LOCAL),
        route=None,
        event_stream=MagicMock(),
    )
    db = MagicMock()
    db.get_session.return_value = None
    authority = SimpleNamespace(
        profile_id="/tmp/fake-home",
        instance_id="inst-1",
        db=db,
        runner=MagicMock(),
        sessions={"sess-1": live},
        events={},
        authorize=MagicMock(),
        attach=AsyncMock(return_value=snap),
    )
    controls = AuthorityConnection(
        authority,
        MagicMock(),
        {"user_id": "uid:1000", "instance_id": "inst-1", "capabilities": ["session:read"]},
    )

    with (
        patch("gateway.session_local_inventory.snapshot_info", new_callable=AsyncMock) as mock_info,
        patch("gateway.session_policy.policy_for_source", return_value=SimpleNamespace(ignore_user_config=False)),
        patch("gateway.session_policy.refresh_profile_hooks") as mock_hooks,
        patch("gateway.session_open.open_local", return_value=None),
        patch("gateway.session_connections.pending_connection", return_value=None),
        patch("gateway.session_server_requests.open_requests", return_value=[]),
        patch("gateway.session_history_mount.write_verdict", return_value={"writable": True}),
        patch("gateway.session_controls._resume_transcript", new_callable=AsyncMock, return_value={"messages": [], "messages_omitted": True}),
        patch("gateway.session_policy_refresh.refresh_settled", new_callable=AsyncMock),
    ):
        res = asyncio.run(controls.resume(ref, {"session_id": "sess-1", "omit_messages": True, "omit_info": True}))

    assert res["session_id"] == "sess-1"
    assert res["revision"] == 7
    mock_info.assert_not_called()
    mock_hooks.assert_not_called()


def test_thread_wake_deliver_scans_threads_of_once(tmp_path, monkeypatch):
    """deliver() fetches threads_of once and shares the launch list between wake_state and _still_working."""
    from gateway import thread_wake
    from hermes_state import SessionDB
    from tools import thread_state as ts

    home = tmp_path / "home"
    home.mkdir()
    db = SessionDB(db_path=home / "state.db")
    now = 1_800_000_000.0
    db._conn.execute(
        "INSERT INTO sessions (id, source, started_at, last_activity_at, archived, title) VALUES (?, 'local', ?, ?, 0, ?)",
        ("lead-1", now - 100, now - 50, "Lead"),
    )
    db._conn.execute(
        "INSERT INTO sessions (id, source, started_at, last_activity_at, archived, title) VALUES (?, 'local', ?, ?, 0, ?)",
        ("child-1", now - 100, now - 10, "Child"),
    )
    db._conn.execute(
        "INSERT INTO work_launches (launch_id, kind, child_profile, child_session_id, launcher_profile, launcher_session_id, title, created_at) "
        "VALUES ('thread:1', 'thread', 'default', 'child-1', 'default', 'lead-1', 'Child', ?)",
        (now - 90,),
    )
    db._conn.execute(
        "INSERT INTO session_admissions (admission_id, request_id, principal_id, target_session_id, lineage_json, "
        "payload_json, payload_digest, intent, status, outcome, owner_epoch, generation) "
        "VALUES ('adm-1', 'thread-1', 'uid:1000', 'child-1', '[]', '{}', 'd', 'queue', 'terminal', 'completed', 1, 1)"
    )
    db._conn.commit()
    db.close()

    monkeypatch.setattr(ts, "homes", lambda: {"default": home})
    monkeypatch.setattr("time.time", lambda: now)
    calls = 0
    orig = ts.threads_of

    def counting_threads_of(*args, **kwargs):
        nonlocal calls
        calls += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(ts, "threads_of", counting_threads_of)
    monkeypatch.setattr(thread_wake, "_submit", lambda *a, **kw: True)

    assert thread_wake.deliver(home, "default", "lead-1") is True
    assert calls == 1

