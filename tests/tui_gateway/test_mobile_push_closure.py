"""Round 9 store contracts: run closure (U13), listed-chat alert policy (U14), payload v2 (U15).

Real SQLite and a fake Apple endpoint only; the clock is injected so idle graces are exact.
"""

import json
import uuid

import pytest

from tests.tui_gateway.test_mobile_push import Sender, delivery as delivery  # noqa: F401
from tui_gateway.mobile_push import Scope
from tui_gateway.mobile_push_payloads import AlertPreview
from tui_gateway.mobile_push_provider import DeliveryResult


def runs(service, scope):
    with service.store._lock:
        return [dict(r) for r in service.store._db.execute(
            "SELECT * FROM runs WHERE scope=? ORDER BY started_at, rowid", (scope.key,))]


def activity_jobs(sender):
    return [job for job in sender.jobs if job["kind"] == "activity"]


def set_run(service, run_id, **columns):
    with service.store.transaction() as db:
        for key, value in columns.items():
            db.execute(f"UPDATE runs SET {key}=? WHERE run_id=?", (value, run_id))


# ── U13 run closure ─────────────────────────────────────────────────────────────


def test_duplicate_start_yields_one_run(delivery):
    service, _, now, _, scope = delivery
    first = service.start_run(scope)
    now[0] += 1
    again = service.start_run(scope, reuse_run_id=first["run_id"])
    assert again["run_id"] == first["run_id"]
    assert [r["run_id"] for r in runs(service, scope)] == [first["run_id"]]


def test_reuse_is_only_for_a_fresh_starting_run(delivery):
    service, _, now, _, scope = delivery
    first = service.start_run(scope)
    service.record(scope, first["run_id"], "work", "thinking")
    second = service.start_run(scope, reuse_run_id=first["run_id"])
    assert second["run_id"] != first["run_id"]
    third_seed = service.start_run(Scope("native", "ops", "other"))
    now[0] += 121
    late = service.start_run(Scope("native", "ops", "other"), reuse_run_id=third_seed["run_id"])
    assert late["run_id"] != third_seed["run_id"]


def test_new_start_silently_closes_the_previous_open_run_and_ends_its_activity(delivery):
    service, sender, now, ids, scope = delivery
    service.register("p", scope, **ids, token="aa" * 32, environment="production")
    first = service.start_run(scope)
    service.register("p", scope, **ids, token="cc" * 32, environment="production",
                     kind="activity", activity_id="live", run_id=first["run_id"])
    service.record(scope, first["run_id"], "work", "thinking")
    now[0] += 30
    service.drain_once()
    sender.jobs.clear()
    second = service.start_run(scope)
    now[0] += 1
    service.drain_once()
    closed, current = runs(service, scope)
    assert (closed["status"], closed["closed_reason"]) == ("cancelled", "superseded")
    assert current["run_id"] == second["run_id"] and current["status"] == "starting"
    assert [job["kind"] for job in sender.jobs] == ["activity"]  # no "stopped" alert
    aps = sender.jobs[0]["payload"]["aps"]
    assert aps["event"] == "end" and aps["content-state"]["currentActivity"] == "Ended"
    # A late terminal for the superseded run is absorbed.
    assert service.record(scope, first["run_id"], "late", "complete") is False


def test_terminal_is_absorbing_and_replay_idempotent(delivery):
    service, sender, _, ids, scope = delivery
    service.register("p", scope, **ids, token="aa" * 32, environment="production")
    run = service.start_run(scope)
    assert service.record(scope, run["run_id"], "turn-finally", "failed") is True
    assert service.record(scope, run["run_id"], "turn-finally", "failed") is False
    assert service.record(scope, run["run_id"], "other", "complete") is False
    service.drain_once()
    assert [job["payload"]["hermex.status"] for job in sender.jobs] == ["failed"]


def test_orphan_sweep_closes_dead_and_legacy_owners_but_keeps_live_runs(delivery):
    service, sender, now, ids, _ = delivery
    store = service.store
    scopes = {name: Scope("native_session", "ops", name) for name in
              ("dead", "legacy", "held-running", "held-idle", "unheld", "fresh")}
    made = {name: service.start_run(scope)["run_id"] for name, scope in scopes.items()}
    for name in ("dead", "legacy", "unheld", "held-running", "held-idle"):
        set_run(service, made[name], updated_at=now[0] - 700)
    set_run(service, made["dead"], owner="999999999:1")
    set_run(service, made["legacy"], owner="")
    service.register("p", scopes["dead"], **ids, token="aa" * 32, environment="production")
    held = {made["held-running"], made["held-idle"]}
    assert store.reconcile_orphans(held, {made["held-running"]}) == 4
    status = {name: runs(service, scope)[0] for name, scope in scopes.items()}
    # Dead process, pre-upgrade row, no session, and a held session idle past 10 minutes.
    assert {n for n, r in status.items() if r["status"] == "cancelled"} == {"dead", "legacy", "unheld", "held-idle"}
    assert status["held-running"]["status"] == "starting"  # a genuinely long live run stays open
    assert status["fresh"]["status"] == "starting"  # inside the idle grace
    assert all(r["closed_reason"] == "orphaned" for n, r in status.items() if r["status"] == "cancelled")
    service.drain_once()
    assert not [job for job in sender.jobs if job["kind"] == "alert"]


def test_orphan_sweep_respects_held_idle_grace_and_unknown_session_view(delivery):
    service, _, now, _, scope = delivery
    run = service.start_run(scope)
    set_run(service, run["run_id"], updated_at=now[0] - 300)
    assert service.store.reconcile_orphans(None, None) == 0  # caller cannot see sessions
    assert service.store.reconcile_orphans({run["run_id"]}, set()) == 0  # held, idle < 600 s
    set_run(service, run["run_id"], updated_at=now[0] - 601)
    assert service.store.reconcile_orphans({run["run_id"]}, set()) == 1
    assert runs(service, scope)[0]["closed_reason"] == "orphaned"


def test_service_sweep_uses_the_gateway_live_view_once_a_minute(delivery):
    service, _, now, _, scope = delivery
    run = service.start_run(scope)
    set_run(service, run["run_id"], updated_at=now[0] - 200)
    service.live_runs = lambda: (set(), set())
    assert service.reconcile(force=True) == 1
    second = service.start_run(Scope("native", "ops", "later"))
    set_run(service, second["run_id"], updated_at=now[0] - 200)
    assert service.reconcile() == 0  # rate limited
    now[0] += 61
    assert service.reconcile() == 1


# ── U14 listed-chat alert policy ────────────────────────────────────────────────


def alerts(sender):
    return [job for job in sender.jobs if job["kind"] == "alert"]


def test_all_chats_policy_alerts_listed_chats_only(delivery):
    service, sender, now, ids, _ = delivery
    service.set_policy("p", **ids, policy="all", token="aa" * 32, environment="production")
    listed, unlisted = Scope("native_session", "ops", "listed"), Scope("native_session", "ops", "telegram")
    for scope, flag in ((listed, True), (unlisted, False)):
        run = service.start_run(scope, listed=flag)
        service.record(scope, run["run_id"], "done", "complete")
    service.drain_once()
    assert [job["payload"]["hermex.destination"]["session_id"] for job in alerts(sender)] == ["listed"]


def test_opened_policy_stops_unopened_chats_and_keeps_explicit_leases(delivery):
    service, sender, _, ids, _ = delivery
    service.set_policy("p", **ids, policy="all", token="aa" * 32, environment="production")
    opened, unopened = Scope("native_session", "ops", "opened"), Scope("native_session", "ops", "unopened")
    service.register("p", opened, **ids, token="aa" * 32, environment="production")
    for scope in (opened, unopened):
        service.start_run(scope, listed=True)
    assert service.set_policy("p", **ids, policy="opened", environment="production") == {
        "policy": "opened", "expires_at": None}
    assert service.store.policy("p", **ids)["policy"] == "opened"
    for scope in (opened, unopened):
        run = service.start_run(scope, listed=True)
        service.record(scope, run["run_id"], "done", "complete")
    service.drain_once()
    assert [job["payload"]["hermex.destination"]["session_id"] for job in alerts(sender)] == ["opened"]


def test_policy_lease_keeps_presence_suppression_and_question_withdrawal(delivery):
    service, sender, now, ids, _ = delivery
    scope = Scope("native_session", "ops", "on-screen")
    service.set_policy("p", **ids, policy="all", token="aa" * 32, environment="production")
    run = service.start_run(scope, listed=True)
    service.store.presence("p", scope, **ids, foreground=True)
    service.record(scope, run["run_id"], "done", "complete")
    service.drain_once()
    assert alerts(sender) == []
    now[0] += 61
    other = Scope("native_session", "ops", "asked")
    run = service.start_run(other, listed=True)
    service.record(other, run["run_id"], "q", "waitingForClarification")
    service.record(other, run["run_id"], "answered", "thinking")
    service.drain_once()
    assert alerts(sender) == []


def test_policy_follows_token_rotation_and_privacy_narrowing(delivery):
    service, sender, _, ids, _ = delivery
    scope = Scope("native_session", "ops", "chat")
    service.set_policy("p", **ids, policy="all", token="aa" * 32, environment="production", preview_enabled=True)
    service.start_run(scope, listed=True)
    options = dict(**ids, environment="production", accepts_scope=lambda _: True)
    service.refresh("p", **options, token=None, categories=["attention"], preview_enabled=False)
    assert service.store.policy("p", **ids)["categories"] == ["attention"]
    assert service.store.policy("p", **ids)["preview_enabled"] is False
    service.refresh("p", **options, token="bb" * 32, categories=["attention", "completion"], preview_enabled=True)
    run = service.start_run(scope, listed=True)
    service.record(scope, run["run_id"], "done", "complete", preview=AlertPreview("Private title", "Private reply"))
    service.drain_once()
    assert alerts(sender)[-1]["token"] == "bb" * 32
    assert alerts(sender)[-1]["payload"]["aps"]["alert"]["body"] == "Private reply"


def test_policy_requires_a_valid_token_and_choice(delivery):
    service, _, _, ids, _ = delivery
    with pytest.raises(ValueError):
        service.set_policy("p", **ids, policy="all", token=None, environment="production")
    with pytest.raises(ValueError):
        service.set_policy("p", **ids, policy="everything", token="aa" * 32, environment="production")
    with pytest.raises(ValueError):
        service.set_policy("p", **ids, policy="all", token="aa" * 32, environment="sandbox")


def test_logout_removes_policy_start_token_and_origins(delivery):
    service, _, _, ids, scope = delivery
    service.set_policy("p", **ids, policy="all", token="aa" * 32, environment="production")
    service.register_start_token("p", **ids, token="ee" * 32, environment="production")
    service.store.set_origin(scope, "p", **ids)
    service.unregister("p", **ids)
    with service.store._lock:
        db = service.store._db
        assert [db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("alert_policies", "activity_start_tokens", "human_origins")] == [0, 0, 0]


# ── U15 presentation payload v2 ─────────────────────────────────────────────────


def register_pair(service, scope, ids, run_id, *, preview_enabled):
    service.register("p", scope, **ids, token="aa" * 32, environment="production", preview_enabled=preview_enabled)
    service.register("p", scope, **ids, token="cc" * 32, environment="production",
                     kind="activity", activity_id="live", run_id=run_id)


def test_working_run_projects_title_step_agents_and_plan(delivery):
    service, sender, now, ids, _ = delivery
    scope = Scope("native_session", "atlas", "chat")
    run = service.start_run(scope)
    register_pair(service, scope, ids, run["run_id"], preview_enabled=True)
    preview = AlertPreview(title="Ship round 9", step="Running tests", agents=2, plan_done=2, plan_total=5)
    service.record(scope, run["run_id"], "tool", "usingTool", preview=preview)
    now[0] += 11
    service.drain_once()
    state = activity_jobs(sender)[-1]["payload"]["aps"]["content-state"]
    assert {k: state[k] for k in ("sessionTitle", "currentStep", "currentActivity", "agentCount", "planCompleted",
                                  "planTotal", "contentVersion", "previewsEnabled", "agentID")} == {
        "sessionTitle": "Ship round 9", "currentStep": "Running tests", "currentActivity": "Running tests",
        "agentCount": 2, "planCompleted": 2, "planTotal": 5, "contentVersion": 2, "previewsEnabled": True,
        "agentID": "atlas"}
    # v1 keys every shipped app decodes are all still present.
    assert {"sessionID", "status", "responseExcerpt", "startedAt", "updatedAt", "isStale", "isFinal"} <= set(state)


def test_step_change_updates_only_the_activity(delivery):
    service, sender, now, ids, _ = delivery
    scope = Scope("native_session", "ops", "chat")
    run = service.start_run(scope)
    register_pair(service, scope, ids, run["run_id"], preview_enabled=True)
    service.record(scope, run["run_id"], "t1", "usingTool", preview=AlertPreview(step="Reading a.py"))
    now[0] += 11
    service.drain_once()
    assert service.record(scope, run["run_id"], "t2", "usingTool", preview=AlertPreview(step="Reading a.py")) is False
    assert service.record(scope, run["run_id"], "t3", "usingTool", preview=AlertPreview(step="Editing b.py")) is True
    now[0] += 11
    service.drain_once()
    assert [job["kind"] for job in sender.jobs] == ["activity", "activity"]
    assert sender.jobs[-1]["payload"]["aps"]["content-state"]["currentStep"] == "Editing b.py"


def test_exact_ask_appears_verbatim_with_previews_on(delivery):
    service, sender, now, ids, _ = delivery
    scope = Scope("native_session", "ops", "chat")
    run = service.start_run(scope)
    register_pair(service, scope, ids, run["run_id"], preview_enabled=True)
    ask = "Allow `rm -rf build/` in the worktree?"
    service.record(scope, run["run_id"], "approval:1", "waitingForApproval", preview=AlertPreview(title="T", ask=ask))
    now[0] += 1
    service.drain_once()
    alert = alerts(sender)[0]["payload"]
    assert alert["aps"]["alert"]["body"] == "Allow rm -rf build/ in the worktree?"
    assert alert["hermex.input_kind"] == "approval"
    state = activity_jobs(sender)[0]["payload"]["aps"]["content-state"]
    assert state["inputKind"] == "approval" and state["currentActivity"].startswith("Allow rm -rf")


def test_completion_carries_first_line_thread_and_agent(delivery):
    service, sender, now, ids, _ = delivery
    scope = Scope("native_session", "iris", "chat")
    run = service.start_run(scope)
    register_pair(service, scope, ids, run["run_id"], preview_enabled=True)
    service.record(scope, run["run_id"], "done", "complete",
                   preview=AlertPreview(title="QA", reply="\n```log\nnoise\n```\nGate B is a **go**.\nDetails follow."))
    now[0] += 1
    service.drain_once()
    alert = alerts(sender)[0]["payload"]
    assert alert["aps"]["alert"] == {"title": "QA", "body": "Gate B is a go."}
    assert alert["aps"]["thread-id"] == scope.key
    assert alert["hermex.agent_id"] == "iris"
    end = activity_jobs(sender)[-1]["payload"]["aps"]["content-state"]
    assert end["responseExcerpt"] == "Gate B is a go." and "agentCount" not in end


def test_previews_off_is_generic_everywhere(delivery):
    service, sender, now, ids, _ = delivery
    scope = Scope("native_session", "default", "chat")
    run = service.start_run(scope)
    register_pair(service, scope, ids, run["run_id"], preview_enabled=False)
    private = AlertPreview(title="Private title", step="Editing secret.md", reply="Private reply", agents=1)
    service.record(scope, run["run_id"], "tool", "usingTool", preview=private)
    now[0] += 11
    service.drain_once()
    service.record(scope, run["run_id"], "done", "complete", preview=private)
    now[0] += 1
    service.drain_once()
    text = json.dumps([job["payload"] for job in sender.jobs])
    assert "Private" not in text and "secret" not in text
    working = activity_jobs(sender)[0]["payload"]["aps"]["content-state"]
    assert working["currentActivity"] == "Ava is working" and working["sessionTitle"] == "Hermex Ava"
    assert working["agentCount"] == 1  # counts are not content


def test_preview_opt_out_while_pending_redacts_queued_activity(delivery):
    service, sender, now, ids, _ = delivery
    scope = Scope("native_session", "ops", "chat")
    run = service.start_run(scope)
    register_pair(service, scope, ids, run["run_id"], preview_enabled=True)
    service.record(scope, run["run_id"], "tool", "usingTool",
                   preview=AlertPreview(title="Private title", step="Editing secret.md"))
    service.refresh("p", **ids, token=None, environment="production", preview_enabled=False,
                    accepts_scope=lambda _: True)
    now[0] += 11
    service.drain_once()
    state = activity_jobs(sender)[0]["payload"]["aps"]["content-state"]
    assert "Private" not in json.dumps(state) and "secret" not in json.dumps(state)
    assert state["previewsEnabled"] is False and "currentStep" not in state


def test_apple_rejection_of_an_activity_does_not_touch_alerts(delivery):
    service, sender, now, ids, _ = delivery
    scope = Scope("native_session", "ops", "chat")
    run = service.start_run(scope)
    register_pair(service, scope, ids, run["run_id"], preview_enabled=False)
    sender.result = DeliveryResult("invalid", "token_invalid")
    service.record(scope, run["run_id"], "w", "thinking")
    now[0] += 11
    service.drain_once()
    sender.result = DeliveryResult("accepted")
    service.record(scope, run["run_id"], "done", "complete")
    service.drain_once()
    assert alerts(sender)


def test_claimed_alert_of_a_closed_run_never_retries(delivery):
    """Review fix: closing a run also drops leased pending rows, so a "retry" cannot resurface."""
    service, sender, now, ids, scope = delivery
    service.register("p", scope, **ids, token="aa" * 32, environment="production")
    run = service.start_run(scope)
    service.record(scope, run["run_id"], "q", "waitingForClarification")
    job = service.store.claim()
    assert job["kind"] == "alert"
    service.start_run(scope)  # the turn was superseded while Apple was being called
    service.store.finish(job, DeliveryResult("retry"))
    now[0] += 60  # past the retry backoff, well inside the alert's one-hour expiry
    service.drain_once()
    assert alerts(sender) == []
