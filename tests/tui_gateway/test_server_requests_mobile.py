"""Canonical registry settlement must be single-owner even with concurrent mobile replies."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from tui_gateway import server_requests as requests


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    requests.reset_for_tests()
    frames, events = [], []
    monkeypatch.setattr(requests, "_write", frames.append)
    monkeypatch.setattr(requests, "_emit", lambda *args: events.append(args))
    yield frames, events
    requests.reset_for_tests()


def test_reply_wins_once_and_is_not_replayed(isolated_registry):
    req = requests.ServerRequest("own", "clarify", {"questions": [{"qid": "q", "question": "Color?"}]}, qids=["q"])
    requests._register(req)
    barrier = threading.Barrier(2)
    def answer(value):
        barrier.wait()
        return requests.lock_answer(req.id, "q", value, expected_sid="own")
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(answer, "Blue"), pool.submit(answer, "Green")
        assert [a.result(), b.result()].count([]) == 1
        assert [a.result(), b.result()].count(None) == 1
    assert req.result["answers"]["q"] in ("Blue", "Green")
    assert req.result["outcome"] == "submitted"
    assert requests.open_requests("own") == []
    assert requests.cancel("own") == 0
    assert req.answered


def test_real_blocking_timeout_rejects_late_answer(isolated_registry):
    frames, events = isolated_registry
    assert requests.send("clarify", "own", {"questions": [{"qid": "q", "question": "Color?"}]},
                         timeout=0, qids=["q"]) == {"answers": {}, "outcome": "timed_out"}
    rid = frames[0]["id"]
    assert events == [("request.cancel", "own", {"id": rid, "method": "clarify", "reason": "timeout"})]
    assert not requests.resolve_response({"id": rid, "result": {"answer": "late"}})
    assert requests.open_requests("own") == []


def test_batch_timeout_preserves_locked_answers(isolated_registry, monkeypatch):
    frames, _ = isolated_registry
    def deliver(frame):
        frames.append(frame)
        assert requests.lock_answer(frame["id"], "a", "Blue", expected_sid="own") == ["b"]
    monkeypatch.setattr(requests, "_write", deliver)
    result = requests.send("clarify", "own", {"questions": [
        {"qid": "a", "question": "A?"}, {"qid": "b", "question": "B?"}]},
        timeout=0, qids=["a", "b"])
    assert result == {"answers": {"a": "Blue"}, "outcome": "timed_out"}


def test_lock_fences_owner_and_type(isolated_registry):
    req = requests.ServerRequest("own", "clarify", {"questions": [{"qid": "a", "question": "A?"}]}, qids=["a"])
    requests._register(req)
    with pytest.raises(PermissionError):
        requests.lock_answer(req.id, "a", "bad", expected_sid="foreign")
    with pytest.raises(PermissionError):
        requests.resolve_response({"id": req.id, "result": {"choice": "once"}},
            expected_sid="own", expected_method="approval", mobile=True)
    assert req.locked == {}
    assert not req.event.is_set()
    assert requests.lock_answer(req.id, "a", "good", expected_sid="own") == []
    assert requests.lock_answer(req.id, "a", "late", expected_sid="own") is None
    assert req.result == {"answers": {"a": "good"}, "outcome": "submitted"}


def test_real_blocking_cancel_is_scoped(isolated_registry, monkeypatch):
    ready = threading.Event()
    monkeypatch.setattr(requests, "_write", lambda frame: ready.set())
    foreign = requests.ServerRequest("foreign", "clarify", {"questions": [{"qid": "q", "question": "Other?"}]}, qids=["q"])
    requests._register(foreign)
    ready.clear()
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(requests.send, "clarify", "own",
                              {"questions": [{"qid": "q", "question": "Own?"}]}, timeout=2, qids=["q"])
        assert ready.wait(1)
        assert requests.cancel("own") == 1
        assert waiting.result(1) == {"answers": {}, "outcome": "cancelled"}
    assert [item["id"] for item in requests.open_requests("foreign")] == [foreign.id]


def test_window_decline_preserves_mobile_owner_fence(isolated_registry, monkeypatch):
    req = requests.ServerRequest("own", "preview.read", {})
    requests._register(req)
    transport = object()
    monkeypatch.setattr(requests, "_clients", lambda sid: [transport])
    frame = {"id": req.id, "error": {"code": requests.NOT_SHOWN_CODE}}
    with pytest.raises(PermissionError):
        requests.resolve_response(frame, transport, expected_sid="foreign")
    assert not req.event.is_set()
    assert not req.declined
    assert requests.resolve_response(frame, transport, expected_sid="own")
    assert req.answered
    assert req.event.is_set()


def test_skipped_batch_answer_preserves_owner_fence(isolated_registry):
    req = requests.ServerRequest("own", "clarify", {}, qids=["a", "b"])
    requests._register(req)
    with pytest.raises(PermissionError):
        requests.lock_answer(req.id, "a", None, expected_sid="foreign")
    assert req.locked == {}
    assert requests.lock_answer(req.id, "a", None, expected_sid="own") == ["b"]
    assert requests.lock_answer(req.id, "b", "Blue", expected_sid="own") == []
    assert req.result == {"answers": {"a": None, "b": "Blue"}, "outcome": "submitted"}
    assert req.event.is_set()
