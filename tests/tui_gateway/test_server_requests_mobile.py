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
    req = requests.ServerRequest("own", "clarify", {"question": "Color?"})
    requests._register(req)
    barrier = threading.Barrier(2)
    def answer(value):
        barrier.wait()
        return requests.resolve_response({"id": req.id, "result": {"answer": value}},
            expected_sid="own", expected_method="clarify", mobile=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(answer, "Blue"), pool.submit(answer, "Green")
        assert sorted([a.result(), b.result()]) == [False, True]
    assert req.result["answer"] in ("Blue", "Green")
    assert requests.open_requests("own") == []
    assert requests.cancel("own") == 0
    assert req.answered


def test_real_blocking_timeout_rejects_late_answer(isolated_registry):
    frames, events = isolated_registry
    assert requests.send("clarify", "own", {"question": "Color?"}, timeout=0) is None
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
    assert result == {"answers": {"a": "Blue"}, "timed_out": True}


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
    assert req.result == {"answers": {"a": "good"}}


def test_real_blocking_cancel_is_scoped(isolated_registry, monkeypatch):
    ready = threading.Event()
    monkeypatch.setattr(requests, "_write", lambda frame: ready.set())
    foreign = requests.ServerRequest("foreign", "clarify", {"question": "Other?"})
    requests._register(foreign)
    ready.clear()
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(requests.send, "clarify", "own", {"question": "Own?"}, timeout=2)
        assert ready.wait(1)
        assert requests.cancel("own") == 1
        assert waiting.result(1) is None
    assert [item["id"] for item in requests.open_requests("foreign")] == [foreign.id]
