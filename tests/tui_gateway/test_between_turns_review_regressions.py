"""Publication, admission and compatibility regressions for between-turns work."""
import copy
import threading
from types import SimpleNamespace

import pytest

from tui_gateway import server
from tests.tui_gateway.test_between_turns_compaction import _fixture


def _block_commit(db, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = db.archive_and_compact

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "archive_and_compact", blocked)
    return entered, release


def test_replaced_agent_never_receives_old_compaction_delta(tmp_path, monkeypatch):
    db, agent, session, before = _fixture(tmp_path, monkeypatch)
    entered, release = _block_commit(db, monkeypatch)
    replacement = SimpleNamespace(context_compressor=object())
    original = replacement.context_compressor
    job = server._start_between_turns_compaction("sid", session, {"completed": True})
    try:
        assert entered.wait(5)
        session["agent"] = replacement
        release.set()
        assert job.done.wait(5)
        assert vars(replacement) == {"context_compressor": original}
        assert session["history"] == before
    finally:
        release.set()
        job.done.wait(5)
        agent.close()
        db.close()


def test_model_switch_serializes_before_live_compressor_mutation(tmp_path, monkeypatch):
    db, agent, session, _ = _fixture(tmp_path, monkeypatch)
    entered, release = _block_commit(db, monkeypatch)
    switched, switch_done = threading.Event(), threading.Event()

    def switch(**kwargs):
        agent.context_compressor.threshold_tokens = 100_000
        agent.model = kwargs["new_model"]
        switched.set()

    monkeypatch.setattr(agent, "switch_model", switch)
    for name in ("_persist_live_session_runtime", "_persist_live_session_system_prompt",
                 "_append_model_switch_marker"):
        monkeypatch.setattr(server, name, lambda *a, **kw: None)
    result = SimpleNamespace(new_model="switched/model", target_provider="test", api_key="",
                             base_url="", api_mode="chat_completions")
    job = server._start_between_turns_compaction("sid", session, {"completed": True})

    def change():
        try:
            server._commit_agent_switch("sid", session, agent, result, "test/model", None)
        finally:
            switch_done.set()

    worker = threading.Thread(target=change)
    try:
        assert entered.wait(5)
        worker.start()
        # On the unfixed tree the mutation completes while the commit is blocked.
        switched.wait(0.2)
        release.set()
        assert switch_done.wait(5)
        assert job.done.wait(5)
        assert agent.model == "switched/model"
        assert agent.context_compressor.threshold_tokens == 100_000
    finally:
        release.set()
        worker.join(5)
        job.done.wait(5)
        agent.close()
        db.close()


@pytest.mark.parametrize("method", ["prompt.submit", "session.steer", "session.interrupt"])
def test_commit_wait_never_blocks_rpc_reader(tmp_path, monkeypatch, method):
    from concurrent.futures import ThreadPoolExecutor

    db, agent, session, _ = _fixture(tmp_path, monkeypatch)
    entered, release = _block_commit(db, monkeypatch)
    reader_returned, response_written = threading.Event(), threading.Event()
    responses = []

    def handle(request):
        if request["params"].get("session_id") == "sid":
            server._preempt_between_turns_compaction(session)
        return {"id": request["id"], "result": {"status": "accepted"}}

    def write(response):
        responses.append(response)
        response_written.set()

    monkeypatch.setitem(server._sessions, "sid", session)
    monkeypatch.setattr(server, "_handle_admitted_request", handle)
    pool = ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(server, "_compaction_rpc_pool", pool, raising=False)
    transport = SimpleNamespace(write=write)
    job = server._start_between_turns_compaction("sid", session, {"completed": True})
    returned = []

    def read():
        returned.append(server.dispatch(
            {"id": "r", "method": method, "params": {"session_id": "sid"}}, transport))
        reader_returned.set()

    reader = threading.Thread(target=read)
    try:
        assert entered.wait(5)
        reader.start()
        assert reader_returned.wait(2), "inline reader waited for the blocked commit"
        assert returned == [None]
        assert not response_written.is_set()
        # The same reader is free to service another chat/response frame.
        assert server.dispatch({"id": "other", "method": "prompt.submit", "params": {"session_id": "other"}},
                               transport) == {"id": "other", "result": {"status": "accepted"}}
        release.set()
        assert response_written.wait(5)
        assert responses == [{"id": "r", "result": {"status": "accepted"}}]
    finally:
        release.set()
        reader.join(5)
        job.done.wait(5)
        pool.shutdown(wait=True)
        agent.close()
        db.close()


@pytest.mark.parametrize("conflict", ["history_edit", "running", "no_fence"])
def test_unfenced_adoption_never_publishes(tmp_path, monkeypatch, conflict):
    from agent import conversation_compression

    db, agent, session, before = _fixture(tmp_path, monkeypatch)
    expected = before[:-2] if conflict == "history_edit" else before

    def adopt(agent_, lease, messages, system_message, **kwargs):
        with session["history_lock"]:
            if conflict == "history_edit":
                session["history"] = expected
                session["history_version"] += 1
            if conflict == "running":
                session["running"] = True
        lease.release()
        return copy.deepcopy(before[-4:]), system_message

    monkeypatch.setattr(conversation_compression, "_adopt_if_parent_rotated", adopt)
    job = server._start_between_turns_compaction("sid", session, {"completed": True})
    try:
        assert job.done.wait(5)
        assert session["history"] == expected
        assert session["history_version"] == (2 if conflict == "history_edit" else 1)
    finally:
        agent.close()
        db.close()


def test_compaction_session_rpc_queue_preserves_input_order(monkeypatch):
    from concurrent.futures import Future

    pending, seen = [], []

    def submit(fn):
        future = Future()
        pending.append((fn, future))
        return future

    monkeypatch.setattr(server, "_pool", SimpleNamespace(submit=submit))
    monkeypatch.setattr(server, "_compaction_rpc_pool", SimpleNamespace(submit=submit), raising=False)
    monkeypatch.setattr(server, "_handle_admitted_request", lambda req: seen.append(req["id"]))
    session = {"agent": SimpleNamespace(compression_between_turns=True),
               "_between_turns_compaction": object()}
    monkeypatch.setitem(server._sessions, "sid", session)
    transport = SimpleNamespace(write=lambda response: None)
    for rid in ("first", "second"):
        server.dispatch({"id": rid, "method": "prompt.submit", "params": {"session_id": "sid"}}, transport)
        # A completed job / hot-reload must not let the second input overtake
        # an already-enqueued FIFO head.
        session.pop("_between_turns_compaction", None)
        session["agent"].compression_between_turns = False
    # The executor is allowed to start runnable tasks in reverse order. Only
    # the queue head may be runnable, so this must not reorder human input.
    while pending:
        fn, future = pending.pop()
        future.set_result(fn())
    assert seen == ["first", "second"]


def test_speculative_pool_cap_survives_detached_worker(tmp_path, monkeypatch):
    from agent.conversation_compression import _try_admit_compression_job, _release_compression_admission

    db, agent, session, _ = _fixture(tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()

    def blocked(messages, **kwargs):
        started.set()
        assert release.wait(10)
        return messages

    agent.context_compressor.compress.side_effect = blocked
    job = server._start_between_turns_compaction("sid", session, {"completed": True})
    other_path = tmp_path / "other"
    other_path.mkdir()
    other_db, other_agent, other, _ = _fixture(other_path, monkeypatch)
    other_key = "other-between-turns-parent"
    other_db.create_session(other_key, source="cli")
    other_db.replace_messages(other_key, other["history"])
    other_agent.session_id = other["session_key"] = other_key
    second = None
    try:
        assert started.wait(5)
        server._preempt_between_turns_compaction(session)
        assert not job.done.is_set()
        second = server._start_between_turns_compaction("other", other, {"completed": True})
        assert second is None, "detached speculative owners must retain the sole opportunistic slot"
        assert _try_admit_compression_job(), "required compression still has reserved capacity"
        _release_compression_admission()
    finally:
        release.set()
        job.done.wait(5)
        if second is not None:
            second.done.wait(5)
        other_agent.close()
        other_db.close()
        agent.close()
        db.close()
