"""Scheduled wakes use the existing owner's mailbox, never a second writer."""
import threading

import pytest

from cron import jobs, scheduler_delivery
from hermes_cli.active_sessions import try_acquire_active_session
from hermes_state import SessionDB
from tools import bot_live_delivery as mailbox
from tui_gateway import session_notifications
from tui_gateway.method_ctx import rebind
from tui_gateway.session_lifecycle import _session_turn_admission


@pytest.fixture
def owner_home(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    with jobs.use_cron_store(home / "cron"):
        db = SessionDB(db_path=home / "state.db")
        db.create_session(session_id="release-chat", source="desktop")
        db.set_session_title("release-chat", "Build review")
        db.append_message("release-chat", "user", "Remember toy-marker-42")
        db.append_message("release-chat", "assistant", "Armed toy-marker-42")
        yield home, db
        db.close()


def test_delivery_validation_is_profile_local_and_atomic(owner_home, tmp_path):
    foreign = SessionDB(db_path=tmp_path / "foreign" / "state.db")
    foreign.create_session(session_id="foreign-chat", source="desktop")
    foreign.close()
    for target in ("session:foreign-chat", "session:missing", "session:", "session:../escape"):
        with pytest.raises(ValueError, match="session:<id>.*same profile"):
            jobs.create_job("Wake", "5m", deliver=target)
    assert jobs.list_jobs(include_disabled=True) == []
    job = jobs.create_job("Wake", "5m", deliver="session:release-chat")
    assert jobs.get_job(job["id"])["deliver"] == "session:release-chat"
    with pytest.raises(ValueError, match="session:<id>.*same profile"):
        jobs.update_job(job["id"], {"deliver": "session:foreign-chat"})
    with pytest.raises(ValueError, match="session:<id>.*same profile"):
        jobs.update_job(job["id"], {"failure_deliver": "session:foreign-chat"})
    assert jobs.get_job(job["id"])["deliver"] == "session:release-chat"
    assert "failure_deliver" not in jobs.get_job(job["id"])


def test_busy_noncanonical_owner_receives_fifo_wakes_once(owner_home, monkeypatch):
    home, db = owner_home
    lease, refusal = try_acquire_active_session(
        session_id="release-chat", surface="desktop", config={}, registry_home=home,
        metadata={"live_session_id": "visible-chat", "bot_live_delivery_consumer": True})
    assert refusal is None
    session = dict(history_lock=threading.RLock(), agent=object(), session_key="release-chat",
                   active_session_lease=lease, running=True)
    submitted = []

    def submit(rid, sid, state, text, **kwargs):
        submitted.append((sid, text))
        db.append_message("release-chat", "user", text)
        db.append_message("release-chat", "assistant", "Resumed toy-marker-42")
        kwargs["terminal_callback"]({"status": "settled", "text": "Resumed toy-marker-42"})
        state["running"] = False
        return True

    poll = rebind(session_notifications._poll_bot_live_delivery_once, {
        "_session_home": lambda state: home,
        "_session_turn_admission": _session_turn_admission,
        "_run_prompt_submit": submit,
        "_notif_release_turn": lambda state: state.update(running=False),
    })
    monkeypatch.setattr(scheduler_delivery._sched, "load_config", dict)
    from gateway import config
    monkeypatch.setattr(config, "load_gateway_config", lambda: None)
    try:
        for run, payload in (("first", "Gate ready"), ("second", "Review complete")):
            job = jobs.create_job("Wake", "5m", name="Owner wake", deliver="session:release-chat")
            job["execution_id"] = run
            assert scheduler_delivery._deliver_result(job, payload) is None
            assert jobs.get_job(job["id"])["last_delivery_queued"]["session:release-chat"]["status"] == "queued"
            assert poll("visible-chat", session) is False
            assert scheduler_delivery._deliver_result(job, payload) is None
        session["running"] = False
        assert poll("visible-chat", session) is True
        assert poll("visible-chat", session) is True
        assert poll("visible-chat", session) is False
        assert submitted == [
            ("visible-chat", '[Scheduled wake from cron job "Owner wake" (first), not the user.]\n\nGate ready'),
            ("visible-chat", '[Scheduled wake from cron job "Owner wake" (second), not the user.]\n\nReview complete'),
        ]
        assert [m["content"] for m in db.get_messages("release-chat")] == [
            "Remember toy-marker-42", "Armed toy-marker-42", submitted[0][1], "Resumed toy-marker-42",
            submitted[1][1], "Resumed toy-marker-42",
        ]
        assert db.get_session_by_title("Bot Chat") is None
    finally:
        lease.release()


def test_deleted_session_fails_preflight_without_creating_a_replacement(owner_home):
    from cron.scheduler_preflight import _preflight_check_delivery
    _home, db = owner_home
    assert _preflight_check_delivery({"deliver": "session:missing"}) == (
        "Invalid session:<id> delivery target 'missing': session must exist in the same profile. "
        "Use an exact ID from `hermes sessions list` in this profile, or select the owning profile first.")
    assert db.get_session("missing") is None


@pytest.mark.parametrize("outcome", ["settled", "ambiguous"])
def test_unsupported_owner_defers_then_claims_once(owner_home, monkeypatch, outcome):
    from cron import bot_chat_delivery as pending, session_delivery
    home, _db = owner_home
    lease, refusal = try_acquire_active_session(
        session_id="release-chat", surface="cli", config={}, registry_home=home)
    assert refusal is None
    turns = []

    def resume(target_home, session_id, message):
        turns.append((target_home, session_id, message))
        return None if outcome == "settled" else "unknown turn outcome"

    monkeypatch.setattr(session_delivery, "_resume_session", resume)
    job = {"id": "toy", "name": "Owner wake", "execution_id": "first"}
    try:
        assert "queued" in session_delivery.deliver_to_session(job, "Gate ready", "release-chat")
        key = job["_bot_chat_delivery_receipts"]["session:release-chat"]["delivery_id"]
        pending.drain()
        assert turns == []
        assert pending.read_pending(key)["status"] == "queued"
    finally:
        lease.release()
    pending.drain()
    assert turns == [(home, "release-chat", '[Scheduled wake from cron job "Owner wake" (first), not the user.]\n\nGate ready')]
    assert pending.read_pending(key)["status"] == outcome
    pending.drain()
    assert len(turns) == 1
    result = session_delivery.deliver_to_session(job, "Gate ready", "release-chat")
    assert result is None if outcome == "settled" else "ambiguous" in result
    assert len(turns) == 1


def test_failure_suppression_is_durable_and_results_still_wake(owner_home):
    from cron import bot_chat_delivery as pending, session_delivery
    home, _db = owner_home
    (home / "config.yaml").write_text("display: {suppress_warning_notifications: true}\n")
    job = {"id": "toy", "name": "Owner wake", "execution_id": "first"}
    assert session_delivery.deliver_to_session(job, "Failed", "release-chat", for_failure=True) is None
    key = job["_bot_chat_delivery_receipts"]["session:release-chat"]["delivery_id"]
    assert pending.read_pending(key)["status"] == "suppressed"
    (home / "config.yaml").write_text("display: {suppress_warning_notifications: false}\n")
    assert session_delivery.deliver_to_session(job, "Failed", "release-chat", for_failure=True) is None
    assert pending.read_pending(key)["status"] == "suppressed"
    assert "queued" in session_delivery.deliver_to_session(job, "Result", "release-chat")
    assert job["_bot_chat_delivery_receipts"]["session:release-chat"]["status"] == "queued"


def test_pending_payload_is_redacted_before_persistence(owner_home):
    from cron import bot_chat_delivery as pending, session_delivery
    job = {"id": "toy", "execution_id": "first"}
    assert "queued" in session_delivery.deliver_to_session(job, "OPENAI_API_KEY=toy-private-value", "release-chat")
    key = job["_bot_chat_delivery_receipts"]["session:release-chat"]["delivery_id"]
    assert pending.read_pending(key)["content"] == "OPENAI_API_KEY=***"


def test_compressed_conversation_targets_its_live_tip(owner_home):
    from cron import session_delivery
    home, db = owner_home
    db.end_session("release-chat", end_reason="compression")
    db.create_session("release-tip", source="desktop", parent_session_id="release-chat")
    db.create_session("unrelated-chat", source="desktop")
    lease, refusal = try_acquire_active_session(
        session_id="release-tip", surface="desktop", config={}, registry_home=home,
        metadata={"live_session_id": "visible-tip", "bot_live_delivery_consumer": True})
    assert refusal is None and lease is not None
    try:
        job = {"id": "toy", "execution_id": "first"}
        assert "queued" in session_delivery.deliver_to_session(job, "Gate ready", "release-chat")
        owner = mailbox.find_session_live_owner(home, "release-chat")
        assert owner["session_id"] == "release-tip"
        assert owner["live_session_id"] == "visible-tip"
        assert mailbox.find_session_live_owner(home, "unrelated-chat") is None
        record = mailbox.claim_pending_delivery(home, owner)
        assert record["message"] == '[Scheduled wake from cron job "toy" (first), not the user.]\n\nGate ready'
        assert mailbox.owner_holds_delivery(home, record) is True
        assert mailbox.claim_pending_delivery(home, owner) is None
    finally:
        lease.release()
    # An accepted claim is not replayed in a CLI after owner loss.
    assert "claimed" in session_delivery.deliver_to_session(job, "Gate ready", "release-chat")
    assert not (home / "cron" / "bot_chat_pending").exists()


@pytest.mark.parametrize("named", [False, True])
def test_detached_child_pins_profile_and_exact_resume(owner_home, monkeypatch, named):
    import subprocess
    from pathlib import Path
    from cron import session_delivery
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    home, _db = owner_home
    target = home / "profiles" / "secondary" if named else home
    target.mkdir(parents=True, exist_ok=True)
    captured = {}

    def run(argv, env, report, timeout):
        captured.update(argv=argv, home=env["HERMES_HOME"], message=Path(argv[-1]).read_text())
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(scheduler_delivery, "_run_bot_chat_turn", run)
    token = set_hermes_home_override(str(target))
    try:
        assert session_delivery._resume_session(target, "release-chat", "Wake") is None
    finally:
        reset_hermes_home_override(token)
    assert captured["home"] == str(target)
    assert captured["message"] == "Wake"
    assert captured["argv"][captured["argv"].index("chat"):] == [
        "chat", "--in", "~", "--resume", "release-chat", "-Q", "--query-file", captured["argv"][-1]]
    assert ("-p" in captured["argv"]) is (not named)


def test_deferred_wake_retains_admitted_home_after_scope_changes(owner_home, monkeypatch, tmp_path):
    from cron import bot_chat_delivery as pending, session_delivery
    home, _db = owner_home
    job = {"id": "toy", "execution_id": "first"}
    assert "queued" in session_delivery.deliver_to_session(job, "Gate ready", "release-chat")
    key = job["_bot_chat_delivery_receipts"]["session:release-chat"]["delivery_id"]
    record = pending.read_pending(key)
    turns = []
    monkeypatch.setattr(session_delivery, "_resume_session", lambda target_home, sid, text: turns.append((target_home, sid)))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "different-root"))
    assert session_delivery.deliver_to_session(dict(record["job"]), "Gate ready", "release-chat", deferred=record) is None
    assert turns == [(home, "release-chat")]
    assert not (tmp_path / "different-root" / "state.db").exists()


def test_same_execution_cannot_replace_a_deferred_payload(owner_home):
    from cron import bot_chat_delivery as pending, session_delivery
    job = {"id": "toy", "execution_id": "first"}
    assert "queued" in session_delivery.deliver_to_session(job, "First", "release-chat")
    key = job["_bot_chat_delivery_receipts"]["session:release-chat"]["delivery_id"]
    result = session_delivery.deliver_to_session(job, "Changed", "release-chat")
    assert result == ("session:release-chat scheduled wake unverified: delivery id already belongs to a different payload; inspect before rearming")
    assert pending.read_pending(key)["content"] == "First"
    assert pending.read_pending(key)["status"] == "queued"
