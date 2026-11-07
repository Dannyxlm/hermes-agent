"""The WebSocket read lane on a dashboard socket (no session authority: the live phone path).

A phone foregrounding sends a burst of a dozen requests over one socket. On the serial lane a slow
read-only projection held every later request on that socket, cheap handshakes included; on the
shared long-handler pool one socket's slow reads took every worker the other clients' long controls
needed. Read-only projections now run beside the serial lane, bounded per socket, on their own pool.

Real ``handle_ws`` and real ``server.dispatch``: only a slow projection's handler body is replaced,
by one that waits on a finite gate (the store/GIL work it stands for). Cheap calls run their real
handlers. Every gate is released and every socket joined before a test ends.
"""
import asyncio
import json
import threading

import pytest

from tests.waiting import async_wait_until
from tui_gateway import server
from tui_gateway import ws as ws_mod
from tui_gateway import ws_read_lane

GATE_S = 10.0  # a gate nobody releases fails the test instead of hanging it


class Socket:
    def __init__(self):
        self.inbound = asyncio.Queue()
        self.replies = {}

    async def accept(self):
        pass

    async def send_text(self, line):
        for part in line.splitlines():
            frame = json.loads(part)
            if frame.get("id") is not None:
                self.replies[frame["id"]] = frame

    async def receive_text(self):
        item = await self.inbound.get()
        if item is None:
            raise ws_mod._WebSocketDisconnect()
        return json.dumps(item)

    async def close(self):
        pass

    def send(self, req_id, method, **params):
        self.inbound.put_nowait({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})


class Gated:
    """Handlers that block on one gate and record what ran, how many at once, and in what order."""

    def __init__(self):
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.running, self.peak, self.peak_by_method, self.log = {}, 0, {}, []

    def handler(self, method, *, gated=True, before=None):
        def run(rid, params):
            with self.lock:
                self.log.append(f"start:{rid}")
                self.running[method] = self.running.get(method, 0) + 1
                self.peak = max(self.peak, sum(self.running.values()))
                self.peak_by_method[method] = max(self.peak_by_method.get(method, 0), self.running[method])
            try:
                if gated:
                    assert self.release.wait(GATE_S), f"{method} gate never released"
                if before is not None:
                    before(rid, params)
            finally:
                with self.lock:
                    self.running[method] -= 1
                    self.log.append(f"done:{rid}")
            # An error envelope: a stand-in result would not match the method's declared result model.
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": 5000, "message": f"stand-in {method}"}}
        return run


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(ws_mod, "_start_backend_liveness", lambda: None)
    monkeypatch.setattr(server, "_ensure_skin_watcher", lambda: None)
    monkeypatch.setattr(server, "_ensure_lease_watcher", lambda: None)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)
    gated, tasks = Gated(), []

    def stand_in(method, **kw):
        monkeypatch.setitem(server._methods, method, gated.handler(method, **kw))

    def connect():
        sock = Socket()
        tasks.append(asyncio.create_task(ws_mod.handle_ws(sock)))
        return sock

    yield gated, stand_in, connect, tasks
    gated.release.set()


async def _close(sockets, tasks):
    for sock in sockets:
        sock.inbound.put_nowait(None)
    done, pending = await asyncio.wait(tasks, timeout=30)
    assert not pending, "a socket never finished its teardown"
    for task in done:
        task.result()


@pytest.mark.asyncio
async def test_cheap_requests_are_answered_while_a_slow_projection_runs(rig):
    """projects.list is an inline handler on the legacy dispatcher: on the serial lane the capability
    handshake and every read behind it waited for it."""
    gated, stand_in, connect, tasks = rig
    stand_in("projects.list")
    sock = connect()
    sock.send("slow", "projects.list")
    await async_wait_until(lambda: "start:slow" in gated.log, timeout=10)
    sock.send("caps", "client.capabilities", server_requests=True)
    sock.send("mobile", "mobile.capabilities")
    sock.send("live", "session.active_list")
    sock.send("ping", "gateway.ping")
    try:
        await async_wait_until(lambda: {"caps", "mobile", "live", "ping"} <= set(sock.replies), timeout=10)
        assert "result" in sock.replies["caps"] and "result" in sock.replies["live"], sock.replies
        assert "slow" not in sock.replies
    finally:
        gated.release.set()
    await async_wait_until(lambda: "slow" in sock.replies, timeout=10)
    await _close([sock], tasks)


@pytest.mark.asyncio
async def test_one_sockets_slow_reads_leave_the_shared_long_pool_to_every_client(rig, monkeypatch):
    """A Desktop window's slow reads took every long-handler worker, so the phone's long control
    (here ``slash.exec``) and its long reads queued behind them. They now run on the read lane's own pool."""
    gated, stand_in, connect, tasks = rig
    family = {"profile": "default", "session_id": "s"}
    slow = [("profiles.list", {}), ("profiles.list", {}), ("family.overview", {}), ("family.overview", {}),
            ("model.options", {}), ("model.options", {}), ("usage.rollup", {}),
            ("skills.changes.list", family), ("session.family", family)]
    for method, _params in slow:
        stand_in(method)
    stand_in("slash.exec", gated=False)
    long_reads = sum(method in server._LONG_HANDLERS for method, _params in slow)
    assert "slash.exec" in server._LONG_HANDLERS and server._rpc_pool_workers <= long_reads
    desk, phone = connect(), connect()
    for n, (method, params) in enumerate(slow):
        desk.send(f"d{n}", method, **params)
    # A control sent after the reads: once it is answered, the serial lane has dispatched every read.
    desk.send("desk-caps", "client.capabilities", server_requests=True)
    try:
        await async_wait_until(lambda: "desk-caps" in desk.replies, timeout=10)
        await async_wait_until(lambda: sum(e.startswith("start:d") for e in gated.log)
                               >= ws_read_lane.LEGACY_LIMITS[ws_read_lane.SLOW], timeout=10)
        phone.send("slash", "slash.exec", session_id="s", command="/help")
        phone.send("live", "session.active_list")
        await async_wait_until(lambda: {"slash", "live"} <= set(phone.replies), timeout=10)
        assert "result" in phone.replies["live"], phone.replies["live"]
    finally:
        gated.release.set()
    await async_wait_until(lambda: len(desk.replies) == len(slow) + 1, timeout=15)
    assert all(r["error"]["message"].startswith("stand-in")
               for rid, r in desk.replies.items() if rid != "desk-caps"), desk.replies
    await _close([desk, phone], tasks)


@pytest.mark.asyncio
async def test_a_socket_runs_a_bounded_number_of_reads_and_of_any_one_method(rig):
    gated, stand_in, connect, tasks = rig
    methods = ["profiles.list", "family.overview", "model.options", "usage.rollup"]
    for method in methods:
        stand_in(method)
    sock = connect()
    # One method polled in a loop first, then the others: its own cap stops it, and the rest fill the socket's.
    burst = [(f"p{n}", methods[0]) for n in range(4)]
    burst += [(f"r{n}", methods[1 + n % (len(methods) - 1)]) for n in range(20)]
    for req_id, method in burst:
        sock.send(req_id, method)
    try:
        cap = min(ws_read_lane.LEGACY_LIMITS[ws_read_lane.SLOW], ws_read_lane.PER_METHOD_LIMIT * len(methods))
        await async_wait_until(lambda: gated.peak >= cap, timeout=10)
        sock.send("ping", "gateway.ping")
        # A fast projection has its own slots: the slow ones filling theirs never hold it up.
        sock.send("live", "session.active_list")
        await async_wait_until(lambda: {"ping", "live"} <= set(sock.replies), timeout=10)
        assert "result" in sock.replies["live"], sock.replies["live"]
        assert gated.peak == cap
        assert max(gated.peak_by_method.values()) == ws_read_lane.PER_METHOD_LIMIT
    finally:
        gated.release.set()
    await async_wait_until(lambda: len(sock.replies) == len(burst) + 2, timeout=15)
    assert gated.peak == cap and max(gated.peak_by_method.values()) == ws_read_lane.PER_METHOD_LIMIT
    await _close([sock], tasks)


@pytest.mark.asyncio
async def test_a_read_sent_after_a_write_sees_it(rig, monkeypatch, tmp_path):
    """Read-after-write on one socket: a slow ``projects.create`` (the real handler, behind a gate)
    then ``projects.list`` (the real read). The read starts only once the write returned."""
    gated, _stand_in, connect, tasks = rig
    real_create = server._methods["projects.create"]
    write_returned = threading.Event()

    def slow_create(rid, params):
        assert gated.release.wait(GATE_S), "create gate never released"
        try:
            return real_create(rid, params)
        finally:
            write_returned.set()

    real_list = server._methods["projects.list"]

    def observed_list(rid, params):
        gated.log.append(f"list-saw-create:{write_returned.is_set()}")
        return real_list(rid, params)

    monkeypatch.setitem(server._methods, "projects.create", slow_create)
    monkeypatch.setitem(server._methods, "projects.list", observed_list)
    folder = tmp_path / "proj"
    folder.mkdir()
    sock = connect()
    sock.send("create", "projects.create", name="Lane", folders=[str(folder)])
    sock.send("list", "projects.list")
    sock.send("ping", "gateway.ping")
    await async_wait_until(lambda: "ping" in sock.replies, timeout=10)
    assert "list" not in sock.replies  # still behind the write it was sent after
    gated.release.set()
    await async_wait_until(lambda: {"create", "list"} <= set(sock.replies), timeout=10)
    assert "result" in sock.replies["create"], sock.replies["create"]
    assert gated.log == ["list-saw-create:True"]
    names = [p["name"] for p in sock.replies["list"]["result"]["projects"]]
    assert "Lane" in names
    await _close([sock], tasks)


@pytest.mark.asyncio
async def test_controls_keep_arrival_order_around_running_reads(rig, monkeypatch):
    """Submit, queue edit and interrupt never reorder: with the submit held, the steer and the Stop sent
    after it wait for it, while reads between them run beside the lane."""
    gated, stand_in, connect, tasks = rig
    stand_in("profiles.list")
    order, lock, submit_may_finish = [], threading.Lock(), threading.Event()

    def control(name, hold=None):
        def run(rid, params):
            with lock:
                order.append(f"start:{rid}")
            if hold is not None:
                assert hold.wait(GATE_S), "submit gate never released"
            with lock:
                order.append(f"done:{rid}")
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": 5000, "message": f"stand-in {name}"}}
        return run

    monkeypatch.setitem(server._methods, "prompt.submit", control("prompt.submit", submit_may_finish))
    for name in ("prompt.steer_queued", "session.interrupt"):
        monkeypatch.setitem(server._methods, name, control(name))
    sock = connect()
    sock.send("read-0", "profiles.list")
    sock.send("submit", "prompt.submit", session_id="s", text="hi")
    sock.send("read-1", "profiles.list")
    sock.send("steer", "prompt.steer_queued", session_id="s", text="more")
    sock.send("stop", "session.interrupt", session_id="s")
    sock.send("ping", "gateway.ping")
    try:
        await async_wait_until(lambda: "ping" in sock.replies and "start:submit" in order, timeout=10)
        await async_wait_until(lambda: sum(e.startswith("start:read") for e in gated.log) == 1, timeout=10)
        assert order == ["start:submit"]  # the steer and the Stop wait behind the held submit
        submit_may_finish.set()
        await async_wait_until(lambda: {"submit", "steer", "stop"} <= set(sock.replies), timeout=10)
        assert order == ["start:submit", "done:submit", "start:steer", "done:steer", "start:stop", "done:stop"]
        assert not {"read-0", "read-1"} & set(sock.replies)  # reads still running beside the lane
    finally:
        submit_may_finish.set()
        gated.release.set()
    await async_wait_until(lambda: {"read-0", "read-1"} <= set(sock.replies), timeout=10)
    await _close([sock], tasks)


@pytest.mark.asyncio
async def test_a_long_read_that_raises_gets_the_long_handler_answer(rig, monkeypatch):
    """The same error frame the long-handler pool wrote, now that the read lane runs it in place."""
    _gated, _stand_in, connect, tasks = rig

    def broken(rid, params):
        raise RuntimeError("roster store gone")

    monkeypatch.setitem(server._methods, "profiles.list", broken)
    sock = connect()
    sock.send("roster", "profiles.list")
    await async_wait_until(lambda: "roster" in sock.replies, timeout=10)
    assert sock.replies["roster"]["error"] == {"code": -32000, "message": "handler error: roster store gone"}
    await _close([sock], tasks)


@pytest.mark.asyncio
async def test_a_retiring_backend_refuses_reads_without_running_them(rig, monkeypatch):
    gated, stand_in, connect, tasks = rig
    stand_in("profiles.list", gated=False)
    stand_in("projects.list", gated=False)
    from hermes_cli.backend_retirement import retirement
    monkeypatch.setattr(retirement, "acquire", lambda: False)
    sock = connect()
    sock.send("long", "profiles.list")
    sock.send("inline", "projects.list")
    await async_wait_until(lambda: {"long", "inline"} <= set(sock.replies), timeout=10)
    assert {sock.replies[k]["error"]["code"] for k in ("long", "inline")} == {5035}
    assert gated.log == []
    await _close([sock], tasks)
