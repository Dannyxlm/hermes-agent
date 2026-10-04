"""Event-published stream projection and atomic reconnect cuts.

Producer inflight state is deliberately NOT the source of this projection: it can
be updated before a corresponding event is published (also true of compute-host
DB writes). Publication and snapshots share this per-runtime lock. Stream text
therefore represents exactly the frames through baseline_seq, never a guessed
latest_seq after an unrelated history read.
"""
import copy
import threading

from .event_replay import latest_seq, replay_epoch

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
                                        "status": "streaming", "start": copy.deepcopy(payload)}
    stream = session.get("_published_stream")
    if stream is None:
        return
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
