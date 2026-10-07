"""Notice presentation is additive: raw/model text remains unchanged."""
import threading

import pytest

from tests.tui_gateway.test_methods_mobile import mobile_home, peer, rpc
from tui_gateway import server, event_replay


@pytest.mark.parametrize("kind", ["async_delegation_complete", "process_complete", "hidden", "internal_notification"])
def test_notice_start_survives_snapshot_and_replay(mobile_home, peer, monkeypatch, kind):
    sid = "notice-runtime"
    session = {"session_key": "fixture", "transport": peer, "history_lock": threading.RLock()}
    server._sessions[sid] = session
    submitted = []
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **kw: submitted.append((a, kw)))
    metadata = {"display_text": "background work finished", "task_count": 1}
    raw = "[ASYNC DELEGATION BATCH COMPLETE — raw envelope]"
    server._notif_submit("rid", sid, session, raw, "notice", display_kind=kind, display_metadata=metadata)
    start = event_replay.events_since(sid, 0)[-1]
    assert start["payload"]["presentation"] == {"display_kind": kind, **metadata}
    snapshot = rpc("session.stream.snapshot", session_id=sid)["result"]
    assert snapshot["stream"]["start"] == start["payload"]
    replay = rpc("session.events.since", session_id=sid, last_seen=0)["result"]
    assert any(frame.get("payload") == start["payload"] for frame in replay["events"])
    assert submitted[0][0][3] == raw
    assert submitted[0][1]["display_metadata"] == metadata
    server._emit("message.start", sid)
    assert "presentation" not in event_replay.events_since(sid, start["seq"])[-1].get("payload", {})


@pytest.mark.parametrize("batch", [False, True])
def test_delegation_result_bodies_come_from_event(mobile_home, peer, monkeypatch, batch):
    result = {"task_index": 2, "status": "failed", "summary": "Actual result", "error": "Failure reason",
              "goal": "Goal preamble", "live_transcript": "/private/transcript.jsonl"}
    event = {"type": "async_delegation", "delegation_id": "d-1", **({"results": [result]} if batch else result)}
    metadata = server._async_delegation_display_metadata(event)
    assert metadata["results"] == [{"task_index": 2, "status": "failed", "body": "Actual result", "error": "Failure reason"}]
    assert "Goal preamble" not in str(metadata["results"])
    assert "/private/transcript.jsonl" not in str(metadata)


def test_process_completion_has_structured_output(mobile_home, peer, monkeypatch):
    import queue
    from types import SimpleNamespace
    event = {"type": "completion", "session_id": "p-1", "exit_code": 1, "output": "Process result"}
    submitted = []
    monkeypatch.setattr("tools.async_delegation.claim_event_delivery", lambda *a: "claim")
    monkeypatch.setattr("tools.async_delegation.complete_event_delivery", lambda *a: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **kw: submitted.append(kw))
    registry = SimpleNamespace(completion_queue=queue.Queue(), is_completion_consumed=lambda sid: False)
    session = {"history_lock": threading.RLock(), "history": []}
    server._notif_dispatch_completions("sid", session, [(event, "Raw process envelope")], registry, None)
    metadata = submitted[0]["display_metadata"]
    assert metadata["results"] == [{"process_id": "p-1", "exit_code": 1, "body": "Process result"}]
    assert metadata["process_ids"] == ["p-1"]
    assert metadata["task_count"] == 1
    assert metadata["failed_count"] == 1


@pytest.mark.parametrize("inflight", [False, True])
@pytest.mark.parametrize("kind,expected", [("async_delegation_complete", "work finished"),
                                          ("process_complete", "work finished"),
                                          ("internal_notification", "work finished"),
                                          ("hidden", "Previous human message"),
                                          (None, "[ASYNC DELEGATION BATCH COMPLETE human text]")])
def test_active_list_previews_use_only_server_typing(mobile_home, peer, monkeypatch, inflight, kind, expected):
    raw = "[ASYNC DELEGATION BATCH COMPLETE human text]"
    row = {"role": "user", "content": raw}
    if kind:
        row.update(display_kind=kind, display_metadata={"display_text": "work finished"})
    history = [{"role": "user", "content": "Previous human message"}]
    session = {"session_key": "fixture", "transport": peer, "history": history, "history_lock": threading.RLock()}
    if inflight:
        session["inflight_turn"] = {**row, "user": raw, "streaming": True}
    else:
        history.append(row)
    server._sessions["preview-runtime"] = session
    result = rpc("session.active_list")["result"]
    item = next(item for item in result["sessions"] if item["id"] == "preview-runtime")
    assert item["preview"] == expected


@pytest.mark.parametrize("kind,expected", [("async_delegation_complete", "work finished"),
                                          ("process_complete", "work finished"),
                                          ("hidden", "Previous human message"),
                                          (None, "[ASYNC DELEGATION BATCH COMPLETE human text]")])
def test_roster_previews_use_only_server_typing(tmp_path, kind, expected):
    from hermes_state import SessionDB
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("s", "desktop")
        db.append_message("s", "user", "Previous human message")
        db.append_message("s", "user", "[ASYNC DELEGATION BATCH COMPLETE human text]",
                          display_kind=kind, display_metadata={"display_text": "work finished"} if kind else None)
        assert server._latest_message_preview(db, "s") == expected


@pytest.mark.parametrize("kind", ["async_delegation_complete", "process_complete"])
def test_notice_history_mobile_resume_and_provider_keep_raw_input(mobile_home, peer, kind):
    import json
    from hermes_state import SessionDB
    from agent.message_metadata import without_persistence_fields
    from tests.tui_gateway.test_methods_mobile import open_bot, scope
    raw = "[ASYNC DELEGATION BATCH COMPLETE — original goal and /private/transcript.jsonl]"
    metadata = {"display_text": "work finished", "results": [{"body": "Actual result"}]}
    with SessionDB(db_path=mobile_home / "profiles" / "ops" / "state.db") as db:
        db.append_message("ops-root", "user", raw, display_kind=kind, display_metadata=metadata)
        db.append_message("ops-root", "user", raw)
        rows = db.get_messages_as_conversation("ops-root")
    projected = server._history_to_messages(rows)
    assert projected[0]["role"] == "user" and projected[0]["text"] == raw
    assert projected[0]["display_kind"] == kind and projected[0]["display_metadata"] == metadata
    assert not projected[1].get("display_kind")
    opened = open_bot(limit=1)
    assert opened["history"]["messages"][0]["text"] == raw
    page = rpc("mobile.snapshot", **scope(opened), limit=1,
               before_row_id=opened["history"]["before_row_id"])["result"]["history"]["messages"][0]
    assert (page["role"], page["text"], page["display_kind"], page["display_metadata"]) == ("user", raw, kind, metadata)
    assert rpc("mobile.bots")["result"]["bots"][1]["canonical_session"]["preview"] == (raw[:80] + "..." if len(raw) > 80 else raw)
    before = {"role": "user", "content": raw, "display_kind": kind,
              "display_metadata": {"display_text": "work finished"}}
    after = {**before, "display_metadata": metadata}
    assert json.dumps(without_persistence_fields(before), sort_keys=True) == json.dumps(without_persistence_fields(after), sort_keys=True)


@pytest.mark.parametrize("kind,expected", [("async_delegation_complete", "work finished"),
                                          ("process_complete", "work finished"), ("hidden", "")])
def test_mobile_and_profile_rosters_do_not_restore_raw_preview(mobile_home, peer, kind, expected):
    from hermes_state import SessionDB
    with SessionDB(db_path=mobile_home / "profiles" / "ops" / "state.db") as db:
        db.append_message("ops-root", "user", "Raw internal envelope", display_kind=kind,
                          display_metadata={"display_text": "work finished"})
    bot = next(bot for bot in rpc("mobile.bots")["result"]["bots"] if bot["profile"] == "ops")
    assert bot["canonical_session"]["preview"] == expected
    profile = next(row for row in rpc("profiles.list")["result"]["profiles"] if row["name"] == "ops")
    assert profile["canonical_session"]["preview"] == expected
    assert profile["last_session"]["preview"] == expected
