#!/usr/bin/env python3
"""Disposable native-client protocol lab, using the real auth and WS dispatcher.

Run with the test environment's Python. Binds only 127.0.0.1, creates its own
temporary HERMES_HOME, uses the existing stub IdP, and refuses outbound sockets
and subprocesses. The hosted driver is real; every model response is a clearly
labelled local fixture. No production credentials or state are used.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CALLBACK = "com.cloudseed.hermex:/oauth/callback"
ROOM_ID = "fixture-team-room"
BOTS = (("default", "Ava", "#DB7778"), ("vox", "Vox", "#AD8FE0"),
        ("iris", "Iris", "#8ACDB4"), ("matisse", "Matisse", "#E0BC68"),
        ("atlas", "Atlas", "#7CB6DE"))


def _deny_external_work(event, args):
    if event == "subprocess.Popen":
        raise RuntimeError("protocol fixture forbids subprocesses")
    if event == "socket.connect":
        address = args[1]
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise RuntimeError("protocol fixture forbids outbound sockets")


def build_lab(home: Path, port: int):
    """Build only inside a directory created by this script's TemporaryDirectory."""
    os.environ["HERMES_HOME"] = str(home)
    os.environ.pop("HERMES_PROFILE", None)
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    import hermes_cli.env_loader
    # A worktree .env is irrelevant to fixtures, including secret-source hydration.
    hermes_cli.env_loader.load_hermes_dotenv = lambda **kwargs: []
    sys.addaudithook(_deny_external_work)

    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    from hermes_state import SessionDB
    from hermes_cli import web_server
    from hermes_cli.dashboard_auth import clear_providers, register_provider
    from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider
    from tests.tui_gateway.test_hosted_room_service import _FakeRPC
    from tui_gateway import server, methods_groups, git_probe
    # Fixture transcripts have no real workspace; do not run host git probes.
    git_probe.run_git = lambda *args: ""
    from tui_gateway.hosted_room_service import HostedRoomService

    for profile, name, color in BOTS:
        path = home if profile == "default" else home / "profiles" / profile
        path.mkdir(parents=True, exist_ok=True)
        path.joinpath("config.yaml").write_text(json.dumps({
            "display_name": name, "description": "Disposable protocol fixture",
            "ui_meta": {"hermes-bots": {"title": name, "color": color,
                "description": "Protocol lab fixture. Local synthetic responses."}},
        }), encoding="utf-8")
        with SessionDB(db_path=path / "state.db") as db:
            key = f"fixture-{profile}-canonical"
            db.create_session(key, "desktop")
            db._conn.execute("UPDATE sessions SET title='Bot Chat' WHERE id=?", (key,))
            db.append_message(key, "user", f"[Fixture] Welcome, {name}. This is a disposable protocol lab.")
            db.append_message(key, "assistant", f"[Fixture] {name} is ready. This transcript is synthetic.")

    server._hermes_home = home
    for name in ("_ensure_skin_watcher", "_start_backend_heartbeat_refresher",
                 "_schedule_startup_orphan_sweep", "_schedule_session_cap_enforcement",
                 "_schedule_agent_build", "_start_session_services", "_schedule_mcp_late_refresh"):
        setattr(server, name, lambda *args, **kwargs: None)

    def no_real_agent(*args, **kwargs):
        raise RuntimeError("protocol fixture forbids real agents")
    server._make_agent = no_real_agent
    server._start_agent_build = lambda *args, **kwargs: None

    def fixture_turn(rid, sid, session, text, **kwargs):
        # The production admission/queue/scope path already ran; only inference is fake.
        with session["history_lock"]:
            with server._session_db(session) as db:
                db.append_message(session["session_key"], "user", text)
                response = f"[Fixture] Received: {text}"
                db.append_message(session["session_key"], "assistant", response)
            session["history"].extend([{"role": "user", "content": text},
                                       {"role": "assistant", "content": response}])
            session["running"] = False
            server._clear_inflight_turn(session)
        server._emit("message.final", sid, {"text": response})
        server._emit("session.info", sid, server._session_info(None, session))
        server._drain_queued_prompt(rid, sid, session)

    server._run_prompt_submit = fixture_turn
    server._run_after_agent_ready = lambda rid, sid, session, text, *_: fixture_turn(rid, sid, session, text)

    class FixtureRPC(_FakeRPC):
        def submit(self, *, profile, on_terminal, **kwargs):
            on_terminal({"status": "settled", "text": f"[Fixture] {profile} received the room message."})
            return {"accepted": True}

    service = HostedRoomService(server, db_path=home / "shared-state.db")
    service.rpc = FixtureRPC()
    service.runtime.rpc = service.rpc
    methods_groups._service = service
    service.create_room(room_id=ROOM_ID, name="Team protocol lab", members=[
        {"member_id": profile, "profile": profile, "handle": name.lower(), "display_name": name}
        for profile, name, _ in BOTS])
    service.start()
    service.send(room_id=ROOM_ID, event_id="fixture-welcome", payload={
        "text": "[Fixture] @ava confirm the shared room is ready.", "thread_id": "fixture-welcome-thread"})

    provider = StubAuthProvider(default_ttl=24 * 3600)
    clear_providers()
    register_provider(provider)
    login = provider.start_login(redirect_uri="http://127.0.0.1/fixture")
    query = parse_qs(urlparse(login.redirect_url).query)
    state = query["state"][0]
    session = provider.complete_login(code="stub_code", state=state,
        code_verifier=provider._state_to_verifier[state], redirect_uri="http://127.0.0.1/fixture")
    credentials = {"access_token": session.access_token, "refresh_token": session.refresh_token,
                   "provider": "stub", "expires_at": session.expires_at}
    web_server.app.state.auth_required = True
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = port
    web_server._DASHBOARD_EMBEDDED_CHAT_ENABLED = True

    lab = FastAPI(title="Disposable native protocol lab")

    # Use a wrapper route so the real application's auth gate stays unchanged.
    async def bootstrap(request: Request):
        if request.client is None or request.client.host != "127.0.0.1":
            raise HTTPException(403, "loopback fixture only")
        return JSONResponse(credentials, headers={"Cache-Control": "no-store"})

    # The Request type must resolve outside build_lab with postponed annotations.
    bootstrap.__annotations__["request"] = Request
    lab.add_api_route("/fixture/native-credentials", bootstrap, methods=["GET"])
    # Synthetic prompt producer only. The request registry and mobile response dispatcher are real.
    prompt_tasks = set()
    async def clarification(request: Request):
        if request.client is None or request.client.host != "127.0.0.1":
            raise HTTPException(403, "loopback fixture only")
        body = await request.json()
        sid = body.get("session_id")
        session = server._sessions.get(sid)
        if not session or not str(session.get("session_key", "")).startswith("fixture-"):
            raise HTTPException(400, "fixture session required")
        from tui_gateway import server_requests
        params = {"questions": [{"qid": "color", "question": "[Fixture] Which color?", "choices": ["Blue", "Green"]},
                                {"qid": "size", "question": "[Fixture] Which size?", "choices": ["Small", "Large"]}]}
        task = asyncio.create_task(asyncio.to_thread(server_requests.send, "clarify", sid,
            params, timeout=60, qids=["color", "size"]))
        prompt_tasks.add(task)
        task.add_done_callback(prompt_tasks.discard)
        return {"fixture": True, "accepted": True}
    clarification.__annotations__["request"] = Request
    lab.add_api_route("/fixture/clarification", clarification, methods=["POST"])

    lab.mount("/", web_server.app)
    return lab, service


async def verify_protocol(base_url: str) -> dict:
    """Use real HTTP, PKCE redemption, single-use ticket headers and two WS peers."""
    import httpx
    import websockets
    verifier = secrets.token_urlsafe(40)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    async with httpx.AsyncClient(base_url=base_url, follow_redirects=False) as http:
        start = await http.get("/auth/native/authorize", params={"provider": "stub", "redirect_uri": CALLBACK,
            "state": "fixture-native-state", "code_challenge": challenge, "code_challenge_method": "S256"})
        assert start.status_code == 302, f"native authorize status {start.status_code}"
        query = parse_qs(urlparse(start.headers["location"]).query)
        callback = await http.get("/auth/callback", params={"code": query["code"][0], "state": query["state"][0]})
        assert callback.status_code == 302
        location = callback.headers["location"]
        assert location.split("?", 1)[0] == CALLBACK
        native = parse_qs(urlparse(location).query)
        assert native["state"] == ["fixture-native-state"]
        response = await http.post("/auth/native/token", json={"code": native["code"][0], "code_verifier": verifier})
        assert response.status_code == 200
        tokens = response.json()
        replay = await http.post("/auth/native/token", json={"code": native["code"][0], "code_verifier": verifier})
        assert replay.status_code == 400
        assert (await http.post("/api/auth/ws-ticket")).status_code == 401
        async def connect():
            minted = await http.post("/api/auth/ws-ticket", headers={"Authorization": f"Bearer {tokens['access_token']}"})
            assert minted.status_code == 200
            ticket = minted.json()["ticket"]
            ws = await websockets.connect(base_url.replace("http://", "ws://") + "/api/ws",
                subprotocols=["hermes-gateway-v1", "hermes-gateway-ticket." + ticket])
            assert ws.subprotocol == "hermes-gateway-v1"
            ready = json.loads(await asyncio.wait_for(ws.recv(), 10))
            assert ready["params"]["type"] == "gateway.ready"
            return ws
        async with contextlib.AsyncExitStack() as stack:
            first, second = await connect(), await connect()
            stack.push_async_callback(first.close)
            stack.push_async_callback(second.close)
            counter = 0
            async def rpc(ws, method, **params):
                nonlocal counter
                counter += 1
                key = str(counter)
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": key, "method": method, "params": params}))
                while True:
                    frame = json.loads(await asyncio.wait_for(ws.recv(), 15))
                    if frame.get("id") == key:
                        return frame
            bots = (await rpc(first, "mobile.bots"))["result"]["bots"]
            assert len(bots) == 5 and all(row["canonical_session"] for row in bots)
            bot = bots[0]
            identity = {"profile": bot["profile"], "canonical_root_id": bot["canonical_session"]["id"]}
            opened = await rpc(first, "mobile.open", **identity)
            assert "result" in opened, opened.get("error")
            one = opened["result"]
            two = (await rpc(second, "mobile.open", **identity))["result"]
            assert one["session_id"] == two["session_id"]
            foreign = await rpc(second, "mobile.open", profile="vox", canonical_root_id=identity["canonical_root_id"])
            assert "error" in foreign
            scoped = {**identity, "session_id": one["session_id"]}
            marker = "[Fixture] Real WebSocket peer proof " + secrets.token_hex(4)
            sent = (await rpc(first, "mobile.submit", **scoped, text=marker))["result"]
            assert sent["accepted"] is True
            for _ in range(50):
                snapshot = (await rpc(second, "mobile.snapshot", **scoped))["result"]
                if any(marker in item["text"] for item in snapshot["history"]["messages"]):
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("second peer did not read accepted canonical message")
            # Disconnect a viewer, then deliver a real upstream request to the remaining peer.
            # A fresh authenticated socket has no runtime authority until it reattaches.
            await first.close()
            first = await connect()
            stack.push_async_callback(first.close)
            assert (await http.post("/fixture/clarification", json={"session_id": one["session_id"]})).status_code == 200
            while True:
                frame = json.loads(await asyncio.wait_for(second.recv(), 10))
                if frame.get("method") == "clarify":
                    break
            request_id = frame["id"]
            native_scope = {**identity, "session_id": one["session_id"]}
            denied = await rpc(first, "mobile.clarify.respond", **native_scope, request_id=request_id, answer="Blue", question_id="color")
            assert "error" in denied, "detached peer must not answer"
            locked = await rpc(second, "mobile.clarify.respond", **native_scope, request_id=request_id, answer="Blue", question_id="color")
            assert locked["result"]["remaining"] == ["size"]
            restored = (await rpc(first, "mobile.open", **identity))["result"]
            assert restored["pending_clarify"]["answers"] == {"color": "Blue"}
            assert restored["open_requests"][0]["id"] == request_id
            accepted = await rpc(first, "mobile.clarify.respond", **native_scope, request_id=request_id, answer="Small", question_id="size")
            assert accepted["result"]["remaining"] == []
            duplicate = await rpc(first, "mobile.clarify.respond", **native_scope, request_id=request_id, answer="Large", question_id="size")
            assert duplicate["result"]["status"] == "expired"
            assert (await rpc(first, "mobile.snapshot", **native_scope))["result"]["pending_clarify"] is None
            state_reply = await rpc(first, "groups.state", room_id=ROOM_ID)
            assert "result" in state_reply, state_reply.get("error")
            state = state_reply["result"]
            assert state["driver_status"]["running"] is True
            event_id = "fixture-proof-" + secrets.token_hex(8)
            payload = {"text": "[Fixture] @ava two-peer room proof", "thread_id": event_id}
            event_one = (await rpc(first, "groups.send", room_id=ROOM_ID, event_id=event_id, payload=payload))["result"]
            event_two = (await rpc(second, "groups.send", room_id=ROOM_ID, event_id=event_id, payload=payload))["result"]
            assert event_one["accepted"] and event_two["accepted"]
            assert event_two["event"]["idempotent"] is True
            assert event_one["event"]["seq"] == event_two["event"]["seq"]
            assert event_one["event"]["event_id"] == event_two["event"]["event_id"]
            log_reply = await rpc(second, "groups.log", room_id=ROOM_ID)
            assert "result" in log_reply, log_reply.get("error")
            log = log_reply["result"]
            events = log["events"]
            assert sum(item["event_id"] == event_one["event"]["event_id"] for item in events) == 1
            return {"fixture": True, "native_pkce": "passed", "request_protocol": "delivered; scoped reply; batch lock; reconnect; duplicate rejected", "native_code_replay": "rejected",
                "unauthenticated_ticket": "rejected", "ws_ticket_transport": "Sec-WebSocket-Protocol",
                "canonical_two_peers": "same runtime and persisted transcript", "foreign_root": "rejected",
                "bots": len(bots), "room_id": ROOM_ID, "hosted_driver": "running with fake executor",
                "room_send_replay": "one durable event", "room_events": len(events)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18787)
    parser.add_argument("--verify", action="store_true", help="Verify an already-running loopback lab")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("use an unprivileged TCP port")
    base_url = f"http://127.0.0.1:{args.port}"
    if args.verify:
        print(json.dumps(asyncio.run(verify_protocol(base_url)), indent=2))
        return
    import uvicorn
    with tempfile.TemporaryDirectory(prefix="hermes-native-protocol-lab-") as directory:
        lab, service = build_lab(Path(directory), args.port)
        print(json.dumps({"fixture": True, "url": base_url,
                          "bootstrap": "/fixture/native-credentials", "room_id": ROOM_ID}), flush=True)
        try:
            uvicorn.run(lab, host="127.0.0.1", port=args.port, lifespan="off", access_log=False, log_level="warning")
        finally:
            service.stop(timeout=3)


if __name__ == "__main__":
    main()
