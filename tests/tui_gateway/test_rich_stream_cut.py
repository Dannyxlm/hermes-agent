"""Rich cuts use real publication, never producer state."""
from tests.tui_gateway.test_methods_mobile import mobile_home, peer, rpc
from tui_gateway import server


def begin(peer):
    sid = "rich-runtime"
    server._sessions[sid] = {"session_key": "fixture", "transport": peer}
    server._emit("message.start", sid)
    return sid


def cut(sid):
    return rpc("session.stream.snapshot", session_id=sid)["result"]["stream"]


def test_interleaved_parts_preserve_publication_order(mobile_home, peer):
    sid = begin(peer)
    for event, payload in [
        ("message.delta", {"text": "a"}), ("message.delta", {"text": "b"}),
        ("reasoning.delta", {"text": "why"}),
        ("tool.start", {"tool_id": "t", "name": "terminal", "context": "pwd", "args": {"command": "pwd"}}),
        ("message.delta", {"text": "after"}),
        ("tool.complete", {"tool_id": "t", "name": "terminal", "duration_s": 1.0, "summary": "failed", "result": {"exit_code": 1}, "inline_diff": "small diff", "todos": []}),
    ]:
        server._emit(event, sid, payload)
    stream = cut(sid)
    assert [p["kind"] for p in stream["parts"]] == ["text", "reasoning", "tool", "text"]
    assert stream["parts"][0] == {"kind": "text", "text": "ab"}
    tool = stream["parts"][2]
    assert tool["status"] == "complete"
    assert tool["error"] is True
    assert tool["args_text"] == '{"command": "pwd"}'
    assert tool["duration_s"] == 1.0 and tool["inline_diff"] == "small diff" and tool["todos"] == []
    assert "result" not in tool
    assert stream["assistant"] == "abafter"


def test_reasoning_replacement_thinking_and_reset(mobile_home, peer):
    sid = begin(peer)
    server._emit("reasoning.delta", sid, {"text": "partial"})
    server._emit("reasoning.available", sid, {"text": "whole"})
    server._emit("thinking.delta", sid, {"text": "status"})
    assert cut(sid)["parts"] == [{"kind": "reasoning", "text": "whole", "complete": True}]
    assert cut(sid)["thinking"] == "status"
    assert cut(sid)["reasoning"] == "status"  # legacy scalar is intentionally unchanged
    server._emit("tool.generating", sid, {"name": "terminal"})
    assert cut(sid)["thinking"] == "terminal"
    server._emit("message.start", sid)
    assert cut(sid)["parts"] == [] and "thinking" not in cut(sid)


def test_rich_capability_has_version(mobile_home, peer):
    caps = rpc("mobile.capabilities")["result"]
    assert "rich_stream_snapshot" in caps["features"]
    assert caps["feature_versions"]["rich_stream_snapshot"] == 1


def test_interim_after_interleaving_never_duplicates_streamed_text(mobile_home, peer):
    sid = begin(peer)
    server._emit("message.delta", sid, {"text": "before"})
    server._emit("tool.start", sid, {"tool_id": "t", "name": "terminal"})
    server._emit("message.delta", sid, {"text": "after"})
    server._emit("message.interim", sid, {"text": "beforeafter", "already_streamed": True})
    server._emit("message.delta", sid, {"text": "next"})
    assert [p["text"] for p in cut(sid)["parts"] if p["kind"] == "text"] == ["before", "after", "next"]


def test_complete_without_start_duplicate_start_and_detached(mobile_home, peer):
    sid = begin(peer)
    server._sessions[sid]["transport"] = server._detached_ws_transport
    server._emit("tool.complete", sid, {"tool_id": "t", "name": "terminal", "summary": "done"})
    for _ in range(2):
        server._emit("tool.start", sid, {"tool_id": "t", "name": "terminal", "preview": "pwd"})
    from tui_gateway.session_stream_cut import stream_cut
    parts = stream_cut(sid, server._sessions[sid])["stream"]["parts"]
    assert len(parts) == 1 and parts[0]["status"] == "complete" and parts[0]["preview"] == "pwd"


def test_parts_caps_keep_newest_utf8_and_reset(mobile_home, peer):
    import json
    from tui_gateway.session_stream_cut import MAX_PARTS, MAX_PART_TEXT_BYTES, MAX_PARTS_BYTES
    sid = begin(peer)
    for i in range(MAX_PARTS + 5):
        server._emit("tool.start", sid, {"tool_id": str(i), "name": "terminal"})
    stream = cut(sid)
    assert len(stream["parts"]) == MAX_PARTS and stream["parts"][0]["tool_id"] == "5"
    assert stream["parts_incomplete"] is True
    server._emit("message.start", sid)
    assert "parts_incomplete" not in cut(sid)
    for i in range(10):
        server._emit("message.delta", sid, {"text": "é" * MAX_PART_TEXT_BYTES})
        server._emit("message.interim", sid, {"text": "é" * MAX_PART_TEXT_BYTES, "already_streamed": True})
    stream = cut(sid)
    assert stream["parts_incomplete"] is True
    assert len(json.dumps(stream["parts"], ensure_ascii=False).encode()) <= MAX_PARTS_BYTES
    assert all(len(p["text"].encode()) <= MAX_PART_TEXT_BYTES for p in stream["parts"])
    assert len(stream["parts"]) < 10


def test_rich_cut_blocks_between_stamp_and_projection(mobile_home, peer, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from tui_gateway import session_stream_cut, event_replay
    sid = begin(peer)
    stamped, release, entered = threading.Event(), threading.Event(), threading.Event()
    original = session_stream_cut.project_event
    def pause(session, frame):
        stamped.set()
        assert release.wait(5)
        original(session, frame)
    monkeypatch.setattr(session_stream_cut, "project_event", pause)
    def snapshot():
        entered.set()
        return session_stream_cut.stream_cut(sid, server._sessions[sid])
    with ThreadPoolExecutor(max_workers=2) as executor:
        producer = executor.submit(server._emit, "tool.start", sid, {"tool_id": "atomic", "name": "terminal"})
        assert stamped.wait(5)
        reader = executor.submit(snapshot)
        assert entered.wait(5) and not reader.done()
        release.set()
        producer.result(5)
        result = reader.result(5)
    assert result["stream"]["parts"][0]["tool_id"] == "atomic"
    assert event_replay.events_since(sid, result["baseline_seq"]) == []


def test_separate_reasoning_phases_stay_in_publication_order(mobile_home, peer):
    sid = begin(peer)
    server._emit("reasoning.delta", sid, {"text": "first"})
    server._emit("message.delta", sid, {"text": "answer"})
    server._emit("reasoning.delta", sid, {"text": "second"})
    server._emit("reasoning.available", sid, {"text": "second complete"})
    assert cut(sid)["parts"] == [
        {"kind": "reasoning", "text": "first"},
        {"kind": "text", "text": "answer"},
        {"kind": "reasoning", "text": "second complete", "complete": True},
    ]


def test_nonstreaming_interim_seals_text(mobile_home, peer):
    sid = begin(peer)
    server._emit("message.interim", sid, {"text": "whole", "already_streamed": False})
    server._emit("message.delta", sid, {"text": "tail"})
    assert cut(sid)["parts"] == [{"kind": "text", "text": "whole"}, {"kind": "text", "text": "tail"}]


def test_connector_publication_is_in_cut_before_cursor_advances(mobile_home, peer):
    from tui_gateway import event_replay
    sid = begin(peer)
    server._emit_tool_lifecycle("tool.start", sid, "manage_connections", {},
        {"tool_id": "connector", "name": "manage_connections", "args": {"token": "private-fixture"}})
    snapshot = rpc("session.stream.snapshot", session_id=sid)["result"]
    part = next(p for p in snapshot["stream"]["parts"] if p.get("tool_id") == "connector")
    assert "private-fixture" not in part["args_text"]
    assert event_replay.events_since(sid, snapshot["baseline_seq"]) == []
    server._emit_tool_lifecycle("tool.complete", sid, "manage_connections", {},
        {"tool_id": "connector", "name": "manage_connections", "result": {"ok": True}})
    assert cut(sid)["parts"][0]["status"] == "complete"
