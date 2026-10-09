"""Round 9 gateway projection: phone origin + push-to-start (U16), run closure (U13) and the
listed-chat alert policy (U14) through the real dispatcher, detached sessions included."""

import json
import threading
import uuid

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_methods_mobile import mobile_home as mobile_home, peer as peer, rpc  # noqa: F401
from tests.tui_gateway.test_methods_mobile_push import push_service as push_service  # noqa: F401
from tests.tui_gateway.test_mobile_session_parity import live, ordinary as ordinary  # noqa: F401
from tui_gateway import server
from tui_gateway.mobile_push import PushService, Scope
from tui_gateway.mobile_push_provider import DeliveryResult


def device(ordinary):
    return {k: ordinary[k] for k in ("installation_id", "connection_id")}


def start_jobs(service):
    return [job for job in service.sender.jobs if job["kind"] == "activity_start"]


def scope_runs(service, scope):
    with service.store._lock:
        return [dict(r) for r in service.store._db.execute(
            "SELECT * FROM runs WHERE scope=? ORDER BY started_at, rowid", (scope.key,))]


ORDINARY = Scope("native_session", "ops", "ordinary-root")


def register_start_token(ordinary, token="ee" * 32):
    response = rpc("mobile.session_activity.start_token.register", **device(ordinary),
                   start_token=token, environment="production")
    assert "result" in response, response
    return response["result"]


def phone_submit(sid, ordinary):
    server._mobile_push_note_submission(server._sessions[sid], {"origin": device(ordinary)})


# ── U16 latest human origin and push-to-start ──────────────────────────────────


def test_continuation_after_phone_input_push_starts_one_activity(push_service, ordinary, mobile_home, peer):
    """AE4: phone sends, the turn ends, a later continuation starts with the app suspended."""
    register_start_token(ordinary)
    sid = live(mobile_home, peer)
    phone_submit(sid, ordinary)
    server._emit("message.start", sid)  # the phone's own turn: the app starts locally
    server._emit("message.complete", sid, {"status": "complete", "text": "done"})
    push_service.drain_once()
    assert start_jobs(push_service) == []
    # A delegation result announces message.start, then _run_prompt_submit announces it again.
    server._emit("message.start", sid)
    server._emit("message.start", sid)
    push_service.drain_once()
    jobs = start_jobs(push_service)
    assert len(jobs) == 1
    aps = jobs[0]["payload"]["aps"]
    run_id = server._sessions[sid]["_mobile_push_run_id"]
    assert (aps["event"], aps["attributes-type"]) == ("start", "AgentRunActivityAttributes")
    assert aps["attributes"]["chatsDestination"] == {
        "version": 1, "surface": "native_session", **device(ordinary), "profile": "ops",
        "session_id": "ordinary-root", "run_id": run_id}
    assert aps["content-state"]["status"] == "starting" and aps["alert"]["body"]
    assert jobs[0]["token"] == "ee" * 32
    assert len(scope_runs(push_service, ORDINARY)) == 2  # one per turn, not per announcement


def test_desktop_input_clears_phone_eligibility(push_service, ordinary, mobile_home, peer):
    """AE5: the latest human message came from Desktop, so no Live Activity starts."""
    register_start_token(ordinary)
    sid = live(mobile_home, peer)
    phone_submit(sid, ordinary)
    server._mobile_push_note_submission(server._sessions[sid], {"text": "from desktop"})
    server._emit("message.start", sid)
    server._emit("message.complete", sid, {"status": "complete", "text": "done"})
    server._emit("message.start", sid)  # continuation
    push_service.drain_once()
    assert start_jobs(push_service) == []
    assert push_service.store.origin(ORDINARY) is None


def test_unauthenticated_or_malformed_origin_never_names_a_phone(push_service, ordinary, mobile_home, peer):
    sid = live(mobile_home, peer)
    server._mobile_push_note_submission(server._sessions[sid], {"origin": {"installation_id": "x", "connection_id": "y"}})
    assert push_service.store.origin(ORDINARY) is None
    peer.auth_identity = None
    server._mobile_push_note_submission(server._sessions[sid], {"origin": device(ordinary)})
    assert push_service.store.origin(ORDINARY) is None


def test_relayed_turns_leave_the_origin_alone(push_service, ordinary, mobile_home, peer):
    sid = live(mobile_home, peer)
    phone_submit(sid, ordinary)
    server._mobile_push_note_submission(server._sessions[sid], {"_turn_author": object()})
    assert push_service.store.origin(ORDINARY)["installation_id"] == ordinary["installation_id"]


def test_queued_phone_origin_survives_a_restart(tmp_path):
    now = [1800000000.0]
    path = tmp_path / "push.sqlite"
    ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    first = PushService(path, _Sender(), clock=lambda: now[0])
    first.store.set_origin(ORDINARY, "p", **ids)
    first.register_start_token("p", **ids, token="ee" * 32, environment="production")
    first.close()
    restarted = PushService(path, _Sender(), clock=lambda: now[0])
    try:
        assert restarted.store.origin(ORDINARY)["installation_id"] == ids["installation_id"]
        restarted.start_run(ORDINARY, continuation=True)
        restarted.drain_once()
        assert [job["kind"] for job in restarted.sender.jobs] == ["activity_start"]
    finally:
        restarted.close()


class _Sender:
    def __init__(self):
        self.jobs, self.result = [], DeliveryResult("accepted")

    def send(self, job):
        self.jobs.append(job)
        return self.result

    def close(self):
        pass


@pytest.fixture
def store_delivery(tmp_path):
    now = [1800000000.0]
    service = PushService(tmp_path / "push.sqlite", _Sender(), clock=lambda: now[0])
    ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    service.store.set_origin(ORDINARY, "p", **ids)
    service.register_start_token("p", **ids, token="ee" * 32, environment="production")
    yield service, now, ids
    service.close()


def test_one_remote_start_per_installation_scope_and_run(store_delivery):
    """KTD8 dedupe: replayed announcements of one run never push-start twice; a phone-sent
    turn (not a continuation) is the app's local start, never a remote one."""
    service, now, ids = store_delivery
    phone_turn = service.start_run(ORDINARY)
    service.store.start_run(ORDINARY, run_id=phone_turn["run_id"], continuation=True)
    later = service.start_run(ORDINARY, continuation=True)
    assert service.store.start_run(ORDINARY, run_id=later["run_id"], continuation=True)["run_id"] == later["run_id"]
    assert service.start_run(ORDINARY, reuse_run_id=later["run_id"], continuation=True)["run_id"] == later["run_id"]
    service.drain_once()
    starts = [job for job in service.sender.jobs if job["kind"] == "activity_start"]
    assert [job["payload"]["aps"]["attributes"]["chatsDestination"]["run_id"] for job in starts] == [later["run_id"]]


def test_revoked_or_rotated_start_token_is_never_used(store_delivery):
    service, now, ids = store_delivery
    service.store.unregister_start_token("p", **ids)
    service.start_run(ORDINARY, continuation=True)
    service.drain_once()
    assert service.sender.jobs == []
    service.register_start_token("p", **ids, token="ee" * 32, environment="production")
    now[0] += 1
    service.start_run(ORDINARY, continuation=True)  # queued under the old token
    service.register_start_token("p", **ids, token="ff" * 32, environment="production")
    service.drain_once()
    assert service.sender.jobs == []


def test_apple_rejected_start_token_is_dropped(store_delivery):
    service, now, ids = store_delivery
    service.sender.result = DeliveryResult("invalid", "token_invalid")
    service.start_run(ORDINARY, continuation=True)
    service.drain_once()
    service.sender.result = DeliveryResult("accepted")
    now[0] += 1
    service.start_run(ORDINARY, continuation=True)
    service.drain_once()
    assert [job["kind"] for job in service.sender.jobs] == ["activity_start"]  # only the rejected one
    with service.store._lock:
        assert service.store._db.execute("SELECT COUNT(*) FROM activity_start_tokens").fetchone()[0] == 0


def test_start_respects_previews_at_enqueue_and_dispatch(store_delivery):
    from tui_gateway.mobile_push_payloads import AlertPreview
    service, now, ids = store_delivery
    service.register("p", ORDINARY, **ids, token="aa" * 32, environment="production", preview_enabled=True)
    service.start_run(ORDINARY, continuation=True, preview=AlertPreview(title="Private title"))
    service.refresh("p", **ids, token=None, environment="production", preview_enabled=False,
                    accepts_scope=lambda _: True)
    service.drain_once()
    aps = [job for job in service.sender.jobs if job["kind"] == "activity_start"][0]["payload"]["aps"]
    assert "Private" not in json.dumps(aps)
    assert aps["attributes"]["sessionTitle"] == "Hermex Ava" and aps["alert"]["title"] == "Ava"


# ── U13 closure through the gateway ────────────────────────────────────────────


def test_exception_in_the_turn_still_closes_the_run(push_service, ordinary, mobile_home, peer):
    sid = live(mobile_home, peer)
    server._emit("message.start", sid)
    server._mobile_push_finish(server._sessions[sid], "error")
    assert scope_runs(push_service, ORDINARY)[-1]["status"] == "failed"
    assert server._sessions[sid]["_mobile_push_status"] == "failed"


def test_restart_that_lost_the_session_closes_orphans_but_not_live_runs(push_service, ordinary, mobile_home, peer):
    push_service.live_runs = server._mobile_push_live_runs
    sid = live(mobile_home, peer)
    server._emit("message.start", sid)
    run_id = server._sessions[sid]["_mobile_push_run_id"]
    with push_service.store.transaction() as db:
        db.execute("UPDATE runs SET updated_at=updated_at-3600 WHERE run_id=?", (run_id,))
    server._sessions[sid]["running"] = True
    assert push_service.reconcile(force=True) == 0  # a long, genuinely live turn stays open
    server._sessions.clear()  # the dashboard lost its session dict
    assert push_service.reconcile(force=True) == 1
    assert scope_runs(push_service, ORDINARY)[-1]["closed_reason"] == "orphaned"


# ── U14 listed-chat policy through the gateway ─────────────────────────────────


def chat(mobile_home, peer, key, source, *, delegate=False):
    path = mobile_home / "profiles" / "ops" / "state.db"
    with SessionDB(db_path=path) as db:
        db.create_session(key, source)
        if delegate:
            db._conn.execute("UPDATE sessions SET model_config=? WHERE id=?",
                             (json.dumps({"_delegate_from": "parent"}), key))
    sid = f"runtime-{key}"
    server._sessions[sid] = {"session_key": key, "profile_home": str(mobile_home / "profiles" / "ops"),
                             "history_lock": threading.RLock(), "transport": peer}
    return sid


@pytest.mark.parametrize("source,delegate,alerts", [
    ("desktop", False, True),      # AE5: typed on Desktop, still alerts under "All chats"
    ("webui", False, True),
    ("desktop", True, False),      # delegate child inheriting the Desktop source
    ("subagent", False, False),
    ("telegram", False, False),
    ("kanban", False, False),
    ("cron", False, False),
])
def test_all_chats_policy_alerts_listed_human_chats_only(push_service, ordinary, mobile_home, peer,
                                                         source, delegate, alerts):
    response = rpc("mobile.session_push.policy", **device(ordinary), policy="all",
                   device_token="aa" * 32, environment="production")
    assert response["result"]["policy"] == "all", response
    sid = chat(mobile_home, peer, f"chat-{source}-{delegate}", source, delegate=delegate)
    server._emit("message.start", sid)
    server._emit("message.complete", sid, {"status": "complete", "text": "done"})
    push_service.drain_once()
    assert bool([job for job in push_service.sender.jobs if job["kind"] == "alert"]) is alerts
    assert rpc("mobile.session_push.policy", **device(ordinary))["result"]["policy"] == "all"
    assert rpc("mobile.session_push.policy", **device(ordinary), policy="opened")["result"] == {
        "policy": "opened", "expires_at": None}


def test_tool_step_and_plan_reach_the_activity(push_service, ordinary, mobile_home, peer):
    sid = live(mobile_home, peer)
    assert "result" in rpc("mobile.session_push.register", **ordinary, preview_enabled=True)
    server._emit("message.start", sid)
    run_id = server._sessions[sid]["_mobile_push_run_id"]
    activity = {**device(ordinary), "profile": "ops", "stored_session_id": "ordinary-root",
                "environment": "production", "activity_token": "cd" * 32, "activity_id": "live", "run_id": run_id}
    assert "result" in rpc("mobile.session_activity.register", **activity)
    server._emit("todo.updated", sid, {"todos": [{"id": "a", "content": "x", "status": "completed"},
                                                {"id": "b", "content": "y", "status": "in_progress"},
                                                {"id": "c", "content": "z", "status": "cancelled"}],
                                      "revision": 1})
    server._emit("tool.start", sid, {"tool_id": "t1", "name": "terminal",
                                     "args": {"command": "scripts/run_tests.sh tests/x.py"}})
    with push_service.store._lock:
        payload = json.loads(push_service.store._db.execute(
            "SELECT payload FROM outbox o JOIN subscriptions s ON o.subscription_id=s.id "
            "WHERE s.kind='activity' AND o.state='pending'").fetchone()[0])
    state = payload["aps"]["content-state"]
    assert (state["status"], state["currentActivity"], state["plan"]) == (
        "usingTool", "Running tests", {"completed": 1, "total": 2})


def test_a_turn_that_ends_before_its_start_push_never_starts_an_activity(store_delivery):
    """Review fix: terminal transitions drop the run's undelivered push-to-start."""
    service, now, ids = store_delivery
    run = service.start_run(ORDINARY, continuation=True)
    service.record(ORDINARY, run["run_id"], "done", "complete")
    service.drain_once()
    assert [job for job in service.sender.jobs if job["kind"] == "activity_start"] == []


def test_queued_phone_turn_is_human_however_long_it_waits(push_service, ordinary, mobile_home, peer):
    """Review fix: a queued phone prompt is never mistaken for a continuation by wall clock."""
    register_start_token(ordinary)
    sid = live(mobile_home, peer)
    session = server._sessions[sid]
    server._mobile_push_note_submission(session, {"origin": device(ordinary)}, "queued")
    session["_mobile_push_human_turns"] = [t - 600 for t in session["_mobile_push_human_turns"]]  # waited 10 min
    server._emit("message.start", sid)
    server._emit("message.start", sid)
    push_service.drain_once()
    assert start_jobs(push_service) == []


def test_steered_input_does_not_mask_the_next_continuation(push_service, ordinary, mobile_home, peer):
    register_start_token(ordinary)
    sid = live(mobile_home, peer)
    session = server._sessions[sid]
    phone_submit(sid, ordinary)
    server._emit("message.start", sid)  # the phone's own turn
    server._mobile_push_note_submission(session, {"origin": device(ordinary)}, "steered")
    server._emit("message.complete", sid, {"status": "complete", "text": "done"})
    server._emit("message.start", sid)  # a later continuation
    push_service.drain_once()
    assert len(start_jobs(push_service)) == 1


def test_start_token_revocation_holds_with_delivery_disabled(push_service, ordinary, mobile_home, peer, monkeypatch):
    register_start_token(ordinary)
    monkeypatch.setattr(server, "_mobile_push_service", lambda: None)
    import tui_gateway.mobile_push as mobile_push
    monkeypatch.setattr(mobile_push, "service_for_home", lambda home: None)
    response = rpc("mobile.session_activity.start_token.unregister", **device(ordinary))
    assert response["result"] == {"removed": 1}
    with push_service.store._lock:
        assert push_service.store._db.execute("SELECT COUNT(*) FROM activity_start_tokens").fetchone()[0] == 0


@pytest.mark.parametrize("redirectable,status", [(False, "queued"), (True, "redirected")])
def test_prompt_submit_reports_accepted_human_input_with_its_origin(monkeypatch, redirectable, status):
    """Wiring: the busy prompt.submit path tells the push projection what it accepted."""
    import types
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    noted = []
    monkeypatch.setattr(server, "_mobile_push_note_submission",
                        lambda session, params, status="streaming": noted.append((params.get("origin"), status)))
    agent = types.SimpleNamespace(_supports_active_turn_redirect=redirectable,
                                  redirect=lambda text: True, interrupt=lambda *a, **k: None)
    session = {"agent": agent, "session_key": "k", "history": [], "history_lock": threading.Lock(),
               "history_version": 0, "running": True, "transport": None, "attached_images": []}
    origin = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    server._sessions["sid-origin"] = session
    try:
        response = server._methods["prompt.submit"]("r", {"session_id": "sid-origin", "text": "hi", "origin": origin})
    finally:
        server._sessions.pop("sid-origin", None)
    assert response["result"]["status"] == status
    assert noted == [(origin, status)]
