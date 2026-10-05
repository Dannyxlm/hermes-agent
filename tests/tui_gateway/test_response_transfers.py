"""Large recovery cuts stay immutable and full fidelity across bounded wire frames."""
import base64
import json
import time

import pytest

from tests.tui_gateway.test_methods_mobile import MobilePeer, mobile_home, peer, rpc
from tui_gateway import event_replay, server, server_requests
from tui_gateway.transport import bind_transport, reset_transport


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def resume():
    return rpc("session.resume", session_id="root", profile="default",
               defer_history=True, omit_messages=True)


def hydrate(descriptor, *, profile="default"):
    data = bytearray()
    scope = {"profile": profile} if profile is not None else {}
    while len(data) < descriptor["byte_length"]:
        reply = rpc("session.response.read", session_id=descriptor["session_id"], **scope,
                    transfer_id=descriptor["transfer_id"], offset=len(data))
        assert len(encoded(reply)) < 4 * 1024 * 1024
        chunk = reply["result"]
        assert chunk["offset"] == len(data)
        decoded = base64.b64decode(chunk["data"], validate=True)
        assert 0 < len(decoded) <= descriptor["chunk_bytes"]
        data.extend(decoded)
        assert chunk["next_offset"] == len(data)
        assert chunk["eof"] == (len(data) == descriptor["byte_length"])
    return json.loads(data)


@pytest.mark.parametrize("method", ["session.stream.snapshot", "session.resume", "session.events.since"])
def test_large_recovery_uses_bounded_frames_without_losing_unicode_or_cut(mobile_home, peer, method):
    event_replay.reset_replay_state()
    sid = resume()["result"]["session_id"]
    server._emit("message.start", sid)
    segment = "é🙂" * 11000
    for _ in range(80):
        server._emit("message.interim", sid, {"text": segment, "already_streamed": False})
    if method == "session.events.since":
        question = server_requests.ServerRequest(sid, "clarify", {"question": segment * 80})
        with server_requests._lock:
            server_requests._open[question.id] = question
    params = {"session_id": "root" if method == "session.resume" else sid, "profile": "default"}
    if method == "session.resume":
        params.update(defer_history=True, omit_messages=True)
    if method == "session.events.since":
        params["last_seen"] = event_replay.latest_seq(sid)
    legacy = rpc(method, **params)
    if method == "session.events.since":
        assert legacy["error"]["code"] == 4413
        assert len(encoded(legacy)) < 4 * 1024 * 1024
        expected = {**event_replay.replay_snapshot(sid, params["last_seen"]),
                    "open_requests": server_requests.open_requests(sid)}
    else:
        assert len(encoded(legacy)) > 4 * 1024 * 1024, "Reproduce the mobile receive-limit failure"
        expected = legacy["result"]
    reply = rpc(method, **params, chunked_response=True)
    assert len(encoded(reply)) < 4 * 1024 * 1024
    descriptor = reply["result"]["response_transfer"]
    # Publication continues after the immutable cut, before the client downloads it.
    server._emit("message.delta", sid, {"text": "after-cut 🧪"})
    restored = hydrate(descriptor)
    assert restored == expected
    if method != "session.events.since":
        cut = restored["stream_snapshot"] if method == "session.resume" else restored
        assert "".join(cut["stream"]["segments"]) == segment * 80
        assert [frame["payload"]["text"] for frame in event_replay.events_since(sid, cut["baseline_seq"])] == ["after-cut 🧪"]
    else:
        assert restored["open_requests"][0]["params"]["question"] == segment * 80
    released = rpc("session.response.release", session_id=sid, profile="default",
                   transfer_id=descriptor["transfer_id"])
    assert released["result"]["released"] is True
    assert "error" in rpc("session.response.read", session_id=sid, profile="default",
                          transfer_id=descriptor["transfer_id"], offset=0)


@pytest.mark.parametrize("scenario", ["foreign-peer", "foreign-profile", "replaced-runtime", "changed-profile",
                                      "detach", "finalize", "expire", "offset", "capacity", "peer-capacity",
                                      "process-capacity", "storage-failure", "small-inline",
                                      pytest.param("private-file", marks=pytest.mark.platforms("posix"))])
def test_transfer_authority_lifetime_and_capacity_are_bounded(mobile_home, peer, monkeypatch, scenario):
    from tui_gateway import response_transfers as rt
    # A small threshold keeps scope/lifecycle cases fast; size fidelity above uses the real budget.
    monkeypatch.setattr(rt, "INLINE_BYTES", 128)
    if scenario == "expire":
        monkeypatch.setattr(rt, "EXPIRY_SECONDS", 1)
    sid = resume()["result"]["session_id"]
    session = server._sessions[sid]
    if scenario == "small-inline":
        monkeypatch.setattr(rt, "INLINE_BYTES", 1024 * 1024)
        baseline = rpc("session.stream.snapshot", session_id=sid)
        assert rpc("session.stream.snapshot", session_id=sid, chunked_response=True) == baseline
        assert not rt.transfers._entries
        return
    if scenario == "capacity":
        monkeypatch.setattr(rt, "MAX_TRANSFER_BYTES", 16)
        assert rpc("session.stream.snapshot", session_id=sid, chunked_response=True)["error"]["code"] == 4413
        assert not rt.transfers._entries
        return
    if scenario == "storage-failure":
        def unavailable(**_kwargs):
            raise OSError("fixture unavailable")
        monkeypatch.setattr(rt.tempfile, "TemporaryFile", unavailable)
        assert rpc("session.stream.snapshot", session_id=sid, chunked_response=True)["error"]["code"] == 4413
        assert not rt.transfers._entries
        return
    result = rpc("session.stream.snapshot", session_id=sid, chunked_response=True)["result"]
    handle = result["response_transfer"]["transfer_id"]
    entry = rt.transfers._entries[handle]
    args = {"session_id": sid, "profile": "default", "transfer_id": handle}
    if scenario == "foreign-peer":
        foreign = MobilePeer()
        server._attach_session_transport(session, foreign)
        token = bind_transport(foreign)
        try:
            for method in ("session.response.read", "session.response.release"):
                params = {**args, **({"offset": 0} if method.endswith("read") else {})}
                assert rpc(method, **params)["error"]["code"] == 4400
        finally:
            reset_transport(token)
        assert hydrate(result["response_transfer"])["session_id"] == sid
    elif scenario == "foreign-profile":
        assert rpc("session.response.read", **{**args, "profile": "ops"}, offset=0)["error"]["code"] == 4400
        assert hydrate(result["response_transfer"])["session_id"] == sid
    elif scenario == "replaced-runtime":
        server._sessions[sid] = dict(session)
    elif scenario == "changed-profile":
        session["profile_home"] = str(mobile_home / "profiles" / "ops")
    elif scenario == "detach":
        server._detach_transport_from_sessions(peer)
        assert entry.file.closed  # cleanup does not wait for another read or expiry
    elif scenario == "finalize":
        server._finalize_session(session)
        assert entry.file.closed
    elif scenario == "expire":
        deadline = time.monotonic() + 3
        while not entry.file.closed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entry.file.closed  # timer cleans idle abandoned transfers too
    elif scenario == "offset":
        for offset in (-1, True, 0.5, "0", entry.size, entry.size + 1):
            assert rpc("session.response.read", **args, offset=offset)["error"]["code"] == 4000
        assert hydrate(result["response_transfer"])["session_id"] == sid
    elif scenario in {"peer-capacity", "process-capacity"}:
        if scenario == "peer-capacity":
            monkeypatch.setattr(rt, "MAX_PEER_TRANSFERS", 1)
        else:
            monkeypatch.setattr(rt, "MAX_PROCESS_BYTES", entry.size)
        assert rpc("session.stream.snapshot", session_id=sid, chunked_response=True)["error"]["code"] == 4413
        assert hydrate(result["response_transfer"])["session_id"] == sid
    elif scenario == "private-file":
        import os
        import stat
        assert stat.S_IMODE(os.fstat(entry.file.fileno()).st_mode) == 0o600
    if scenario in {"foreign-peer", "foreign-profile", "offset", "peer-capacity", "process-capacity", "private-file"}:
        assert rpc("session.response.release", **args)["result"]["released"] is True
    assert "error" in rpc("session.response.read", **args, offset=0)
    assert entry.file.closed
    assert handle not in rt.transfers._entries


@pytest.mark.parametrize("chunked", [False, True], ids=["inline", "transferred"])
def test_recovery_uses_existing_surrogate_wire_policy_without_mutating_source(mobile_home, peer, chunked):
    from hermes_state import SessionDB
    from tui_gateway.ws import _sanitize_ws_text
    sid = resume()["result"]["session_id"]
    dirty = "valid é🙂 / lone \ud83d / low \udc00" + ("x" * (1024 * 1024) if chunked else "")
    question = server_requests.ServerRequest(sid, "clarify", {"question": dirty})
    with server_requests._lock:
        server_requests._open[question.id] = question
    with SessionDB(mobile_home / "state.db", read_only=True) as db:
        before = db.get_messages_as_conversation("root")
    legacy = rpc("session.events.since", session_id=sid, last_seen=0)
    expected = json.loads(_sanitize_ws_text(json.dumps(legacy, ensure_ascii=False)))["result"]
    reply = rpc("session.events.since", session_id=sid, last_seen=0, chunked_response=True)
    if chunked:
        descriptor = reply["result"]["response_transfer"]
        result = hydrate(descriptor, profile=None)
        assert rpc("session.response.release", session_id=sid,
                   transfer_id=descriptor["transfer_id"])["result"]["released"] is True
    else:
        assert "response_transfer" not in reply["result"]
        result = json.loads(_sanitize_ws_text(json.dumps(reply, ensure_ascii=False)))["result"]
    assert result == expected
    assert result["open_requests"][0]["params"]["question"].startswith("valid é🙂 / lone � / low �")
    assert question.params["question"] == dirty
    with SessionDB(mobile_home / "state.db", read_only=True) as db:
        assert db.get_messages_as_conversation("root") == before


def test_named_profile_recovery_inherits_attached_scope_across_profile_switches(mobile_home, peer):
    from hermes_state import SessionDB
    homes = {"default": mobile_home, "ops": mobile_home / "profiles" / "ops"}
    stored = {"default": "root", "ops": "ops-root"}
    runtimes, original_history = {}, {}
    for profile, home in homes.items():
        with SessionDB(home / "state.db") as db:
            db.append_message(stored[profile], "user", "durable " + profile)
            original_history[profile] = db.get_messages_as_conversation(stored[profile])
        runtimes[profile] = rpc("session.resume", session_id=stored[profile], profile=profile,
                                defer_history=True, omit_messages=True)["result"]["session_id"]
    for profile in ("default", "ops", "default"):
        sid = rpc("session.resume", session_id=stored[profile], profile=profile,
                  defer_history=True, omit_messages=True)["result"]["session_id"]
        assert sid == runtimes[profile]
        baseline = event_replay.latest_seq(sid)
        text = (profile + " é🙂") * 160_000
        server._emit("message.start", sid)
        server._emit("message.delta", sid, {"text": text})
        other_sid = runtimes["ops" if profile == "default" else "default"]
        for method in ("session.stream.snapshot", "session.events.since"):
            cursor = {"last_seen": baseline} if method == "session.events.since" else {}
            reply = rpc(method, session_id=sid, chunked_response=True, **cursor)
            descriptor = reply["result"]["response_transfer"]
            for foreign_method in ("session.response.read", "session.response.release"):
                offset = {"offset": 0} if foreign_method.endswith("read") else {}
                refusal = rpc(foreign_method, session_id=other_sid,
                              transfer_id=descriptor["transfer_id"], **offset)
                assert refusal["error"]["code"] == 4400
            restored = hydrate(descriptor, profile=None)
            if method == "session.stream.snapshot":
                assert restored["session_id"] == sid
                assert restored["stream"]["assistant"] == text
            else:
                assert [frame["payload"]["text"] for frame in restored["events"]
                        if frame["type"] == "message.delta"] == [text]
            assert rpc("session.response.release", session_id=sid,
                       transfer_id=descriptor["transfer_id"])["result"]["released"] is True
    for profile, home in homes.items():
        with SessionDB(home / "state.db", read_only=True) as db:
            assert db.get_messages_as_conversation(stored[profile]) == original_history[profile]


def test_detach_during_serialization_closes_spool_and_revokes_handle(mobile_home, peer, monkeypatch):
    import contextvars
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from tui_gateway import response_transfers as rt
    sid = resume()["result"]["session_id"]
    server._emit("message.start", sid)
    server._emit("message.delta", sid, {"text": "x" * (1024 * 1024)})
    entered, finish_write, detaching = threading.Event(), threading.Event(), threading.Event()
    original_file, original_forget = rt.tempfile.TemporaryFile, rt.transfers.forget
    opened = []

    class PausedSpool:
        def __init__(self, file):
            self.file = file
        def write(self, data):
            if not entered.is_set():
                entered.set()
                assert finish_write.wait(10)
            return self.file.write(data)
        def __getattr__(self, name):
            return getattr(self.file, name)

    def paused_file(**kwargs):
        file = original_file(**kwargs)
        opened.append(file)
        return PausedSpool(file)

    def detach(**kwargs):
        detaching.set()
        return original_forget(**kwargs)

    monkeypatch.setattr(rt.tempfile, "TemporaryFile", paused_file)
    monkeypatch.setattr(rt.transfers, "forget", detach)
    with ThreadPoolExecutor(max_workers=2) as pool:
        read = pool.submit(contextvars.copy_context().run, rpc, "session.stream.snapshot",
                           session_id=sid, chunked_response=True)
        try:
            assert entered.wait(10)
            disconnected = pool.submit(server._detach_transport_from_sessions, peer)
            assert detaching.wait(10)
        finally:
            finish_write.set()
        reply = read.result(timeout=10)
        disconnected.result(timeout=10)
    assert reply["error"]["code"] == 4400
    assert opened and all(file.closed for file in opened)
    assert not rt.transfers._entries
    assert not rt.transfers._preparing


def test_spool_close_error_cannot_skip_session_finalization_or_other_spools(mobile_home, peer, monkeypatch, caplog):
    import threading
    from tui_gateway import response_transfers as rt
    monkeypatch.setattr(rt, "INLINE_BYTES", 128)
    sid = resume()["result"]["session_id"]
    descriptors = [rpc("session.stream.snapshot", session_id=sid, chunked_response=True)["result"]["response_transfer"]
                   for _ in range(2)]
    entries = [rt.transfers._entries[item["transfer_id"]] for item in descriptors]
    files = [entry.file for entry in entries]

    class FailingClose:
        def close(self):
            files[0].close()
            raise OSError("fixture close failure")

    entries[0].file = FailingClose()
    session = server._sessions[sid]
    session["resume_history_ready"] = ready = threading.Event()
    try:
        server._finalize_session(session)
        assert session["_finalized"] is True
        assert ready.is_set() and session["resume_history_error"] == "session resume cancelled"
        assert all(file.closed for file in files)
        assert all(item["transfer_id"] not in rt.transfers._entries for item in descriptors)
        assert "recovery transfer spool close failed" in caplog.text
        assert all(item["transfer_id"] not in caplog.text for item in descriptors)
    finally:
        rt.transfers.forget(session=session)
        for file in files:
            file.close()


def test_legacy_open_request_overflow_is_explicit_and_bounded(mobile_home, peer):
    sid = resume()["result"]["session_id"]
    request = server_requests.ServerRequest(sid, "clarify", {"question": "x" * (4 * 1024 * 1024)})
    with server_requests._lock:
        server_requests._open[request.id] = request
    reply = rpc("session.events.since", session_id=sid, last_seen=event_replay.latest_seq(sid))
    assert reply["error"]["code"] == 4413
    assert len(encoded(reply)) < 1024
    assert server_requests.open_requests(sid)[0]["params"]["question"] == request.params["question"]


def test_reads_extend_idle_expiry_past_original_deadline(mobile_home, peer, monkeypatch):
    from tui_gateway import response_transfers as rt
    monkeypatch.setattr(rt, "INLINE_BYTES", 128)
    monkeypatch.setattr(rt, "EXPIRY_SECONDS", 1)
    sid = resume()["result"]["session_id"]
    descriptor = rpc("session.stream.snapshot", session_id=sid, chunked_response=True)["result"]["response_transfer"]
    args = {"session_id": sid, "transfer_id": descriptor["transfer_id"], "offset": 0}
    entry = rt.transfers._entries[descriptor["transfer_id"]]
    time.sleep(0.65)
    assert "result" in rpc("session.response.read", **args)
    time.sleep(0.65)
    assert "result" in rpc("session.response.read", **args), "active reads must outlive creation plus 120 seconds"
    deadline = time.monotonic() + 3
    while not entry.file.closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert entry.file.closed, "an idle transfer must still be reclaimed"


def test_serialization_does_not_block_another_peers_cleanup(mobile_home, peer, monkeypatch):
    import contextvars
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from tui_gateway import response_transfers as rt
    monkeypatch.setattr(rt, "INLINE_BYTES", 128)
    sid = resume()["result"]["session_id"]
    other = MobilePeer()
    server._attach_session_transport(server._sessions[sid], other)
    foreign_token = bind_transport(other)
    try:
        foreign_descriptor = rpc("session.stream.snapshot", session_id=sid, chunked_response=True)["result"]["response_transfer"]
    finally:
        reset_transport(foreign_token)
    foreign_file = rt.transfers._entries[foreign_descriptor["transfer_id"]].file
    writing, proceed, cleaned = threading.Event(), threading.Event(), threading.Event()
    original = rt.tempfile.TemporaryFile

    class PausedFile:
        def __init__(self):
            self.file = original(mode="w+b")

        def write(self, data):
            writing.set()
            assert proceed.wait(5)
            return self.file.write(data)

        def __getattr__(self, name):
            return getattr(self.file, name)

    monkeypatch.setattr(rt.tempfile, "TemporaryFile", lambda **_kwargs: PausedFile())
    context = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=2) as pool:
        creating = pool.submit(context.run, lambda: rpc("session.stream.snapshot", session_id=sid, chunked_response=True))
        assert writing.wait(3)
        def cleanup():
            rt.transfers.forget(transport=other)
            cleaned.set()
        removing = pool.submit(cleanup)
        try:
            assert cleaned.wait(0.5), "unrelated peer cleanup must not wait for serialization"
        finally:
            proceed.set()
        assert "result" in creating.result(timeout=5)
        removing.result(timeout=5)
    assert foreign_file.closed


@pytest.mark.parametrize("deferred", [False, True])
def test_failed_resume_does_not_start_auto_continue_or_leave_new_attachment(mobile_home, peer, monkeypatch, deferred):
    from tui_gateway import response_transfers as rt
    starts = []
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_start_session_work", lambda *args, **kwargs: starts.append((args, kwargs)) or object())
    monkeypatch.setattr(server, "read_turn_marker", lambda *args: {
        "started_at": time.time(), "attempts": 0, "prompt": "Original task", "writer_pid": -1})
    monkeypatch.setattr(server, "marker_writer_state", lambda marker: "dead")
    monkeypatch.setattr(server, "_auto_continue_config", lambda: (True, 900, 2))
    if deferred:
        def hydrate_and_recover(sid, key, db, **kwargs):
            record = server._sessions[sid]
            record["resume_hydrating"] = False
            record["resume_history_ready"].set()
            server._maybe_schedule_auto_continue(sid, record, key)
            if kwargs.get("close_db"):
                db.close()
        monkeypatch.setattr(server, "_schedule_resume_hydration", hydrate_and_recover)
    monkeypatch.setattr(rt, "MAX_TRANSFER_BYTES", 16)
    failed = rpc("session.resume", session_id="root", profile="default", defer_history=deferred,
                 chunked_response=True)
    assert failed["error"]["code"] == 4413
    assert not starts, "serialization refusal must precede starting any recovery turn"
    sid, session = next(iter(server._sessions.items()))
    assert not server._session_transport_contains(session, peer)
    monkeypatch.setattr(rt, "MAX_TRANSFER_BYTES", 64 * 1024 * 1024)
    accepted = rpc("session.resume", session_id="root", profile="default", defer_history=deferred,
                   chunked_response=True)
    assert accepted["result"]["session_id"] == sid
    assert accepted["result"]["auto_continue"]["attempt"] == 1
    assert len(starts) == 1
    assert server._session_transport_contains(session, peer)


def test_resume_quota_preflight_does_not_construct_runtime(mobile_home, peer, monkeypatch):
    from tui_gateway import response_transfers as rt
    monkeypatch.setattr(rt, "MAX_PEER_TRANSFERS", 0)
    assert rpc("session.resume", session_id="root", profile="default", defer_history=True,
               chunked_response=True)["error"]["code"] == 4413
    assert not server._sessions


def test_failed_resume_rolls_back_only_new_peer_membership(mobile_home, peer, monkeypatch):
    from tui_gateway import response_transfers as rt
    sid = resume()["result"]["session_id"]
    session = server._sessions[sid]
    monkeypatch.setattr(rt, "MAX_TRANSFER_BYTES", 16)
    assert rpc("session.resume", session_id="root", profile="default", chunked_response=True)["error"]["code"] == 4413
    assert server._session_transport_contains(session, peer), "preexisting membership is not rolled back"
    other = MobilePeer()
    token = bind_transport(other)
    try:
        assert rpc("session.resume", session_id="root", profile="default", chunked_response=True)["error"]["code"] == 4413
    finally:
        reset_transport(token)
    assert server._session_transport_contains(session, peer)
    assert not server._session_transport_contains(session, other)


def test_pending_writer_contention_is_retryable_and_released(mobile_home, peer, monkeypatch):
    from tui_gateway import response_transfers as rt
    monkeypatch.setattr(rt, "MAX_PEER_TRANSFERS", 1)
    reservation = rt.transfers.reserve(peer)
    try:
        assert rpc("session.resume", session_id="root", profile="default", chunked_response=True)["error"]["code"] == 4414
        assert not server._sessions
    finally:
        rt.transfers.cancel(reservation)
    assert "result" in rpc("session.resume", session_id="root", profile="default", defer_history=True, chunked_response=True)
