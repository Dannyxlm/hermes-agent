"""Event-published stream projection and atomic reconnect cuts.

Producer inflight state is deliberately NOT the source of this projection: it can
be updated before a corresponding event is published (also true of compute-host
DB writes). Publication and snapshots share this per-runtime lock. Stream text
therefore represents exactly the frames through baseline_seq, never a guessed
latest_seq after an unrelated history read.
"""
import copy
import threading
import json

from .event_replay import latest_seq, replay_epoch

MAX_PARTS = 200
MAX_PART_TEXT_BYTES = 64 * 1024
MAX_PARTS_BYTES = 512 * 1024
MAX_DISPLAY_BYTES = 4096


def _tail(text, limit):
    return text.encode("utf-8")[-limit:].decode("utf-8", errors="ignore")


def _rich_event(session, stream, event, payload):
    parts = stream["parts"]
    if event in {"message.delta", "message.interim", "tool.start", "tool.complete"}:
        session["_rich_reasoning_open"] = None
    if event in {"thinking.delta", "tool.generating"}:
        value = payload.get("text") if event == "thinking.delta" else payload.get("name")
        stream["thinking"] = _tail(str(value or ""), MAX_DISPLAY_BYTES)
    if event == "message.delta":
        if session.get("_rich_text_open") is None:
            session["_rich_text_open"] = {"kind": "text", "text": ""}
            parts.append(session["_rich_text_open"])
        session["_rich_text_open"]["text"] += str(payload.get("text") or "")
    elif event in {"reasoning.delta", "reasoning.available"}:
        session["_rich_text_open"] = None
        part = session.get("_rich_reasoning_open")
        if part is None:
            part = {"kind": "reasoning", "text": ""}
            parts.append(part)
        if event == "reasoning.available":
            part.update(text=str(payload.get("text") or ""), complete=True)
            session["_rich_reasoning_open"] = None
        else:
            part["text"] += str(payload.get("text") or "")
            session["_rich_reasoning_open"] = part
    elif event in {"tool.start", "tool.complete"} and payload.get("tool_id"):
        session["_rich_text_open"] = None
        tool_id = str(payload["tool_id"])
        part = next((p for p in parts if p["kind"] == "tool" and p["tool_id"] == tool_id), None)
        if part is None:
            part = {"kind": "tool", "tool_id": tool_id, "name": str(payload.get("name") or ""),
                    "status": "running"}
            parts.append(part)
        # Duplicate starts merge metadata but cannot resurrect a completed card.
        for key in ("context", "preview", "args_text", "summary", "inline_diff"):
            if isinstance(payload.get(key), str):
                value = payload[key]
                if len(value.encode("utf-8")) > MAX_DISPLAY_BYTES:
                    stream["parts_incomplete"] = True
                part[key] = _tail(value, MAX_DISPLAY_BYTES)
        if "args_text" not in part and isinstance(payload.get("args"), dict):
            value = json.dumps(payload["args"], ensure_ascii=False)
            part["args_text"] = _tail(value, MAX_DISPLAY_BYTES)
            if len(value.encode("utf-8")) > MAX_DISPLAY_BYTES:
                stream["parts_incomplete"] = True
        for key in ("labels", "todos"):
            value = payload.get(key)
            if isinstance(value, list):
                bounded = []
                for item in value:
                    if len(json.dumps(bounded + [item], ensure_ascii=False).encode("utf-8")) > MAX_PART_TEXT_BYTES:
                        stream["parts_incomplete"] = True
                        break
                    bounded.append(copy.deepcopy(item))
                part[key] = bounded
        if event == "tool.complete":
            from . import server
            part["status"] = "complete"
            result = payload.get("result")
            part["error"] = server._tool_result_needs_user(result if isinstance(result, str) else json.dumps(result))
            if isinstance(payload.get("duration_s"), (int, float)):
                part["duration_s"] = payload["duration_s"]
    elif event == "message.interim":
        part = session.get("_rich_text_open")
        text = str(payload.get("text") or stream["assistant"])
        if not payload.get("already_streamed"):
            if part is None:
                if text:
                    parts.append({"kind": "text", "text": text})
            elif payload.get("text"):
                part["text"] = str(payload["text"])
        session["_rich_text_open"] = None

    for part in parts:
        if part["kind"] in {"text", "reasoning"} and len(part["text"].encode("utf-8")) > MAX_PART_TEXT_BYTES:
            part["text"] = _tail(part["text"], MAX_PART_TEXT_BYTES)
            stream["parts_incomplete"] = True
    while len(parts) > MAX_PARTS or len(json.dumps(parts, ensure_ascii=False).encode("utf-8")) > MAX_PARTS_BYTES:
        removed = parts.pop(0)
        for key in ("_rich_text_open", "_rich_reasoning_open"):
            if session.get(key) is removed:
                session[key] = None
        stream["parts_incomplete"] = True

_lock_creation = threading.Lock()


def publication_lock(session):
    with _lock_creation:
        return session.setdefault("_stream_publication_lock", threading.RLock())


def project_event(session, frame):
    """Caller holds publication_lock, after replay stamping and before transport delivery."""
    if frame.get("method") != "event":
        return
    params = frame.get("params", {})
    event, payload = params.get("type"), params.get("payload") or {}
    if not isinstance(payload, dict):
        return
    if event == "message.start":
        session["_published_stream"] = {"start_seq": params["seq"], "segments": [], "assistant": "",
                                        "status": "streaming", "start": copy.deepcopy(payload), "parts": []}
        session["_rich_text_open"] = session["_rich_reasoning_open"] = None
    stream = session.get("_published_stream")
    if stream is None:
        return
    _rich_event(session, stream, event, payload)
    if event == "message.delta":
        stream["assistant"] += str(payload.get("text") or "")
    elif event == "message.interim":
        # Interim seals the currently streaming segment; already_streamed means
        # replace, not append its copy. A later delta belongs to a fresh segment.
        stream["segments"].append(str(payload.get("text") or stream["assistant"]))
        stream["assistant"] = ""
    elif event == "message.complete":
        stream["assistant"] = str(payload.get("text") or "")
        stream["status"] = payload.get("status") or "complete"
        stream["terminal"] = copy.deepcopy(payload)
    elif event == "thinking.delta":
        stream["reasoning"] = stream.get("reasoning", "") + str(payload.get("text") or "")
    elif event == "todo.updated":
        stream["todo_state"] = copy.deepcopy(payload)


def stream_cut(sid, session):
    """Copy while holding the SAME lock that stamps and projects every session frame."""
    with publication_lock(session):
        return {"session_id": sid, "stored_session_id": session.get("session_key"),
                "epoch": replay_epoch(), "baseline_seq": latest_seq(sid),
                "stream": copy.deepcopy(session.get("_published_stream"))}
