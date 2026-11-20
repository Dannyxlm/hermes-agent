"""Regression tests for thread orchestration performance, thread-turn background safety,
uninitialized profile-store resilience, skills cache isolation, and subagent output_schema guards."""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


def test_skills_cache_isolates_profiles_and_kanban_and_invalidates_on_write(tmp_path):
    """_SKILLS_CACHE keys on active skills dir + kanban selection: multiplexed profiles and
    CLI kanban scans stay cached without evicting each other, reflect new SKILL.md files
    immediately via directory signature, and respect clear_skills_cache()."""
    from gateway import session_local_inventory as inv
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools import skills_tool

    home_a = tmp_path / "profile_a"
    home_b = tmp_path / "profile_b"
    for h, sname in ((home_a, "alpha-skill"), (home_b, "beta-skill")):
        sdir = h / "skills" / "ops" / sname
        sdir.mkdir(parents=True)
        (sdir / "SKILL.md").write_text(f"---\nname: {sname}\ndescription: test\n---\nbody\n", encoding="utf-8")

    skills_tool.clear_skills_cache()
    scan_calls = 0
    orig_read = skills_tool._read_skill_text

    def counting_read(path):
        nonlocal scan_calls
        scan_calls += 1
        return orig_read(path)

    try:
        with patch.object(skills_tool, "_read_skill_text", side_effect=counting_read):
            tok_a = set_hermes_home_override(str(home_a))
            try:
                a1 = skills_tool.find_all_skills()
                c1 = inv._cli_skills()
                before_warm = scan_calls
                a2 = skills_tool.find_all_skills()
                c2 = inv._cli_skills()
                assert scan_calls == before_warm  # warm hits for both default and kanban views
            finally:
                reset_hermes_home_override(tok_a)

            # Switch to profile_b and back to profile_a: neither should evict the other
            tok_b = set_hermes_home_override(str(home_b))
            try:
                b1 = skills_tool.find_all_skills()
            finally:
                reset_hermes_home_override(tok_b)

            before_return = scan_calls
            tok_a = set_hermes_home_override(str(home_a))
            try:
                a3 = skills_tool.find_all_skills()
                assert scan_calls == before_return  # still cached after visiting profile_b

                # Adding a new SKILL.md invalidates immediately via directory signature
                new_dir = home_a / "skills" / "ops" / "gamma-skill"
                new_dir.mkdir(parents=True)
                (new_dir / "SKILL.md").write_text(
                    "---\nname: gamma-skill\ndescription: fresh\n---\nbody\n", encoding="utf-8"
                )
                a4 = skills_tool.find_all_skills()
                assert any(s["name"] == "gamma-skill" for s in a4)
            finally:
                reset_hermes_home_override(tok_a)
    finally:
        skills_tool.clear_skills_cache()

    assert [s["name"] for s in a1] == [s["name"] for s in a2] == [s["name"] for s in a3]
    assert any(s["name"] == "alpha-skill" for s in a1)
    assert any(s["name"] == "beta-skill" for s in b1)
    assert c1 == c2


def test_thread_turn_disables_background_notify_while_non_thread_keeps_it():
    """Tool-sent thread turns (request_id starting with 'thread-') disable post-turn
    notify_on_complete so threads don't stall at done; non-thread turns (chat -q, lead-chat
    thread_update) keep normal async delivery."""
    from gateway.session_context import clear_session_vars, set_session_vars
    from gateway.session_display import submission_turn_scope
    from tools.terminal_tool_background import _apply_async_support

    tokens = set_session_vars(
        platform="local", chat_id="local-1", session_key="local-1", async_delivery=True
    )
    try:
        # 1. Tool-sent thread turn -> notify disabled
        proc_thread = SimpleNamespace(id="proc_thread", watcher_platform="")
        with submission_turn_scope("adm-1", "thread-create-abc"):
            rd: dict = {"status": "running", "notify_on_complete": True}
            notify, watch = _apply_async_support(proc_thread, rd, True, ["READY"])
        assert notify is False
        assert watch is None
        assert rd["notify_on_complete"] is False
        assert "poll" in rd.get("notify_unsupported", "").lower()
        assert proc_thread.watcher_platform == ""

        # 2. Lead-chat thread_update turn -> notify preserved
        proc_lead = SimpleNamespace(id="proc_lead", watcher_platform="")
        with submission_turn_scope("adm-2", "thread_update:xyz"):
            rd2: dict = {"status": "running", "notify_on_complete": True}
            notify2, watch2 = _apply_async_support(proc_lead, rd2, True, ["READY"])
        assert notify2 is True
        assert watch2 == ["READY"]
        assert "notify_unsupported" not in rd2
    finally:
        clear_session_vars(tokens)


def test_thread_wake_skips_uninitialized_profile_store_without_work_launches(tmp_path, monkeypatch):
    """An uninitialized/empty profile state.db missing work_launches must not crash
    thread_wake.leads() or thread_state.threads_of() for healthy profiles."""
    from gateway import thread_wake
    from tools import thread_state as ts

    good_home = tmp_path / "good"
    bad_home = tmp_path / "bad"
    good_home.mkdir()
    bad_home.mkdir()

    bad_conn = sqlite3.connect(bad_home / "state.db")
    bad_conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    bad_conn.commit()
    bad_conn.close()

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


def test_output_schema_skips_failed_api_turn_but_retries_max_iterations_handoff():
    """API failures (failed=True / error set) skip the output_schema retry turn, while a
    non-failed max-iterations handoff (completed=False, failed=False) still gets one retry."""
    from tools.delegate_tool_child_run import _validate_child_output_schema

    schema = {"type": "object", "required": ["verdict"]}

    # 1. API failure -> 0 retries
    child_fail = MagicMock()
    child_fail._delegate_output_schema = schema
    failed_result = {
        "completed": False,
        "failed": True,
        "error": "Anthropic rate-limited every one of 3 attempts",
        "final_response": "Anthropic rate-limited every one of 3 attempts — temporarily unavailable.",
        "api_calls": 3,
    }
    outcome_fail = _validate_child_output_schema(child_fail, failed_result, 0, "child-0", None)
    assert outcome_fail.retries == 0
    assert outcome_fail.valid is False
    child_fail.run_conversation.assert_not_called()

    # 2. Max-iterations non-failure handoff -> still gets 1 schema retry
    child_handoff = MagicMock()
    child_handoff._delegate_output_schema = schema
    child_handoff.run_conversation.return_value = {
        "completed": True,
        "final_response": '{"verdict": "PASS"}',
        "api_calls": 1,
    }
    handoff_result = {
        "completed": False,
        "failed": False,
        "final_response": "Ran out of iterations, summary: looks good.",
        "api_calls": 50,
    }
    outcome_handoff = _validate_child_output_schema(child_handoff, handoff_result, 0, "child-1", None)
    assert outcome_handoff.retries == 1
    assert outcome_handoff.valid is True
    child_handoff.run_conversation.assert_called_once()


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


def test_resume_lightweight_falls_back_against_older_owner():
    """If an older owner rejects omit_info with 4001 invalid_params, _resume_lightweight retries once without it."""
    from hermes_cli.gateway_client import GatewayRPCError
    from tools.thread_tool import _resume_lightweight

    calls = []

    class _FakeClient:
        async def rpc(self, method, **kwargs):
            calls.append((method, kwargs))
            if kwargs.get("omit_info"):
                raise GatewayRPCError("invalid_params", code=4001, data={"reason": "invalid_params", "field": "omit_info"})
            return {"session_id": "sess-1", "revision": 3}

    snap = asyncio.run(_resume_lightweight(_FakeClient(), "sess-1"))
    assert snap == {"session_id": "sess-1", "revision": 3}
    assert len(calls) == 2
    assert calls[0][1].get("omit_info") is True
    assert "omit_info" not in calls[1][1]


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
