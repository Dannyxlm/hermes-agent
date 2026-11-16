"""Paper cards under ``dashboard.turn_isolation``: a turn that runs in the compute-host child draws cards
when the app is attached in the serving process.

The app sends ``session.create`` and the first ``prompt.submit`` back to back, so the first prompt beats
the in-process agent pre-build and runs in the child. The child builds its own agent before it has a
session record, behind a pipe transport that never advertised anything, so app-made chats never had
``show_card`` (Iris R1, 2026-11-16). The serving process now says on each turn frame whether a drawing
client is attached.

Each test advertises through the real ``client.capabilities`` RPC, creates through the real
``session.create``, builds the child's agent through the real ``ComputeHost`` and ``_make_agent`` (a real
``AIAgent``), and admits turns through ``_prepare_turn_input``, stopping right after the turn-start syncs.
"""
from __future__ import annotations

import io

import pytest

from hermes_constants import get_hermes_home
from tools.registry import invalidate_check_fn_cache
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.transport import bind_transport, reset_transport

KINDS = ["table", "flight", "route", "place", "weather", "chart", "document", "link", "gallery", "compose"]


class _Admitted(Exception):
    pass


class Peer:
    def write(self, _frame):
        return True

    def close(self):
        return None


def _config(cards: bool):
    (get_hermes_home() / "config.yaml").write_text(f"cards:\n  enabled: {str(cards).lower()}\n", encoding="utf-8")
    invalidate_check_fn_cache()


@pytest.fixture
def isolated(monkeypatch):
    """Turn isolation on, cards on, agents built against an unreachable stub provider; returns the
    ``platform_override`` of every ``_make_agent`` call."""
    _config(True)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda *_a: {"turn_isolation": True})
    runtime = {"provider": "custom", "base_url": "http://127.0.0.1:9/v1", "api_key": "test-key",
               "api_mode": "chat_completions"}
    monkeypatch.setattr(server, "_resolve_agent_model_runtime", lambda *_a: ("test-model", dict(runtime)))
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda platform=None: ["file"])
    # The race the app always wins: its first prompt arrives before the in-process pre-build.
    monkeypatch.setattr(server, "_schedule_agent_build", lambda sid, delay=0.05: None)
    builds = []
    real_make = server._make_agent

    def counted(*args, **kwargs):
        builds.append(kwargs.get("platform_override"))
        return real_make(*args, **kwargs)

    monkeypatch.setattr(server, "_make_agent", counted)
    before = set(server._sessions)
    yield builds
    for sid in set(server._sessions) - before:
        session = server._sessions.pop(sid)
        for record in (session, session.get("_child") or {}):
            if record.get("agent") is not None:
                record["agent"].close()
    invalidate_check_fn_cache()


def _rpc(peer, method, **params):
    token = bind_transport(peer)
    try:
        reply = server.handle_request({"id": method, "method": method, "params": params})
    finally:
        reset_transport(token)
    assert "error" not in reply, reply
    return reply["result"]


def _app_creates_a_chat(cards=True) -> tuple[str, dict]:
    """The app's socket: advertise, then ``session.create`` with the fields the app sends."""
    peer = Peer()
    if cards:
        _rpc(peer, "client.capabilities", server_requests=True, cards={"v": 1, "kinds": KINDS})
    created = _rpc(peer, "session.create", profile="default", idempotency_key=f"create-{id(peer)}",
                   close_on_disconnect=False)
    sid = created["session_id"]
    return sid, server._sessions[sid]


def _turn_in_child(host: ComputeHost, sid: str, parent: dict) -> dict:
    """What the serving process sends for this turn, received by a child that is another process: the
    child sees only its own session record (``parent["_child"]``), never the serving process's."""
    assert server._session_uses_compute_host(parent)
    frame = server._compute_host_turn_frame("turn", sid, parent, "Make me a made-up monthly budget")
    if parent.get("_child") is None:
        server._sessions.pop(sid)
    else:
        server._sessions[sid] = parent["_child"]
    try:
        parent["_child"] = host._ensure_server_session(server, frame)
    finally:
        server._sessions[sid] = parent
    return parent["_child"]


def _admit(monkeypatch, sid, session):
    """Turn admission up to (and including) every turn-start sync; stop before history is snapshotted."""
    monkeypatch.setattr(server, "_profile_runtime_scope_tokens", lambda profile_home, **_kw: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    for name in ("_apply_pending_model_switch", "_sync_agent_model_with_config", "_sync_agent_compression_with_config",
                 "_sync_agent_fallback_with_config", "_sync_bot_capabilities"):
        monkeypatch.setattr(server, name, lambda sid, session: None)

    def stop(_session):
        raise _Admitted()

    monkeypatch.setattr(server, "_adopt_out_of_band_turns", stop)
    st = server._TurnRun(agent=None, one_turn_restore=None, terminal_callback=None, receipt_committed=False)
    previous = server._sessions.get(sid)
    server._sessions[sid] = session
    try:
        with pytest.raises(_Admitted):
            server._prepare_turn_input(sid, session, st, "hello", [])
    finally:
        server._release_turn_scopes(sid, session, st)
        server._sessions[sid] = previous
    return session["agent"]


def _has_cards(agent) -> bool:
    return "show_card" in agent.valid_tool_names


@pytest.fixture
def host():
    child = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    yield child
    child.close()


def test_a_new_app_chats_first_turn_in_the_child_has_show_card(monkeypatch, isolated, host):
    sid, parent = _app_creates_a_chat()
    child = _turn_in_child(host, sid, parent)
    assert _has_cards(child["agent"]) and "read_file" in child["agent"].valid_tool_names
    assert _admit(monkeypatch, sid, child) is child["agent"]  # built right the first time: no second build
    assert isolated == ["tui"]


def test_a_chat_no_drawing_client_is_attached_to_gets_no_cards_in_the_child(monkeypatch, isolated, host):
    sid, parent = _app_creates_a_chat(cards=False)  # a terminal or Desktop that never advertised
    child = _turn_in_child(host, sid, parent)
    assert not _has_cards(child["agent"])
    assert _admit(monkeypatch, sid, child) is child["agent"] and len(isolated) == 1


def test_flag_off_leaves_the_childs_tool_array_byte_identical(monkeypatch, isolated, host):
    _config(False)
    drawing_sid, drawing = _app_creates_a_chat()
    plain_sid, plain = _app_creates_a_chat(cards=False)
    with_app = _turn_in_child(host, drawing_sid, drawing)["agent"]
    without = _turn_in_child(host, plain_sid, plain)["agent"]
    assert with_app.tools == without.tools and not _has_cards(with_app)
    assert _admit(monkeypatch, drawing_sid, drawing["_child"]) is with_app and len(isolated) == 2


def test_a_chat_living_in_the_child_gets_cards_the_turn_after_the_app_attaches(monkeypatch, isolated, host):
    """Home: its runtime session already runs in the child, built while only a terminal was attached."""
    sid, parent = _app_creates_a_chat(cards=False)
    child = _turn_in_child(host, sid, parent)
    built = child["agent"]
    phone = Peer()
    _rpc(phone, "client.capabilities", cards={"v": 1, "kinds": KINDS})
    parent["transport"] = phone  # the app opens Home: its socket is now the session's transport
    assert _turn_in_child(host, sid, parent) is child and child["agent"] is built  # the frame changes no tools
    rebuilt = _admit(monkeypatch, sid, child)
    assert rebuilt is not built and _has_cards(rebuilt) and len(isolated) == 2
    assert _turn_in_child(host, sid, parent) is child
    assert _admit(monkeypatch, sid, child) is rebuilt and len(isolated) == 2  # once, not every turn


def test_a_new_app_chat_built_in_process_has_show_card_on_its_first_turn(monkeypatch, isolated):
    """The other side of the race: the pre-build finished first, so the turn stays in this process."""
    sid, session = _app_creates_a_chat()
    server._start_agent_build(sid, session)
    assert session["agent_ready"].wait(60) and session["agent_error"] is None
    assert not server._session_uses_compute_host(session)
    assert _has_cards(session["agent"])
    assert _admit(monkeypatch, sid, session) is session["agent"] and len(isolated) == 1
