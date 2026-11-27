import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hermes_cli.web_routers.chat_ws import _is_legacy_mobile_ws, gateway_ws
from tui_gateway.ws import _authority_connection


def _fake_ws(*, user_agent: str = "", authority=None, ticket_home=None, auth_identity=None):
    scope = {}
    headers = {"user-agent": user_agent} if user_agent else {}
    app = SimpleNamespace(state=SimpleNamespace(session_authority=authority))
    return SimpleNamespace(
        scope=scope,
        headers=headers,
        app=app,
        _hermes_auth_identity=auth_identity,
        _hermes_ticket_profile_home=ticket_home,
        _hermes_ws_subprotocol=None,
        close=AsyncMock(),
    )


def test_is_legacy_mobile_ws_matches_only_unbound_hermes_mobile():
    assert _is_legacy_mobile_ws(
        _fake_ws(user_agent="HermesMobile/2026112603 CFNetwork/3896.300.71.2.4 Darwin/27.3.0"),
        ticket_home=None,
    ) is True
    # Profile-bound webapp tickets stay canonical even if UA were spoofed.
    assert _is_legacy_mobile_ws(
        _fake_ws(user_agent="HermesMobile/2026112603 CFNetwork/3896.300.71.2.4 Darwin/27.3.0"),
        ticket_home="/home/ubuntu/.hermes/profiles/iris",
    ) is False
    # Hermes.app macOS (Electron) and Hermes Web (Chrome/Safari) stay canonical.
    assert _is_legacy_mobile_ws(
        _fake_ws(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Hermes/0.0.0 Chrome/152.0.7977.169 Electron/44.10.2 Safari/537.36"),
        ticket_home=None,
    ) is False
    assert _is_legacy_mobile_ws(
        _fake_ws(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/157.0.0.0 Safari/537.36"),
        ticket_home=None,
    ) is False
    assert _is_legacy_mobile_ws(_fake_ws(user_agent=""), ticket_home=None) is False


def test_authority_connection_respects_disabled_scope_flag():
    authority = MagicMock()
    authority.profile_id = "/tmp/hermes"
    authority.instance_id = "inst-1"
    transport = MagicMock()

    ws_disabled = _fake_ws(authority=authority)
    ws_disabled.scope["hermes.session_authority_disabled"] = True
    assert _authority_connection(ws_disabled, transport, {"user_id": "danny", "provider": "basic"}, True) is None

    ws_enabled = _fake_ws(authority=authority)
    with patch("gateway.session_controls.AuthorityConnection", return_value="canonical-conn") as mock_ctor:
        conn = _authority_connection(ws_enabled, transport, {"user_id": "danny", "provider": "basic"}, True)
    assert conn == "canonical-conn"
    mock_ctor.assert_called_once()


def test_gateway_ws_disables_session_authority_only_for_hermes_mobile():
    authority = MagicMock()
    authority.profile_id = "/tmp/hermes"
    authority.instance_id = "inst-1"
    authority.runner = None
    identity = {"user_id": "danny", "provider": "basic"}

    async def _run(ua: str):
        ws = _fake_ws(user_agent=ua, authority=authority, auth_identity=identity)
        captured = {}

        async def _fake_handle_ws(sock, *, auth_identity=None, operator=False, subprotocol=None):
            captured["disabled"] = sock.scope.get("hermes.session_authority_disabled", False)
            captured["identity"] = auth_identity
            captured["operator"] = operator

        with (
            patch("hermes_cli.web_routers.chat_ws._close_unless_sidecar_allowed", AsyncMock(return_value=True)),
            patch("hermes_cli.mcp_startup.start_deferred_mcp_discovery_now", lambda: None),
            patch("gateway.session_authorities.owner_scope", lambda _auth: MagicMock(__enter__=lambda s: None, __exit__=lambda s, *a: None)),
            patch("tui_gateway.ws.handle_ws", side_effect=_fake_handle_ws),
        ):
            await gateway_ws(ws)
        return captured

    ios = asyncio.run(_run("HermesMobile/2026112603 CFNetwork/3896.300.71.2.4 Darwin/27.3.0"))
    assert ios["disabled"] is True
    assert ios["identity"]["user_id"] == "danny"
    assert ios["identity"]["provider"] == "basic"
    assert ios["identity"]["profile_scope"] == "host"
    assert ios["operator"] is True

    desktop = asyncio.run(_run("Mozilla/5.0 Hermes/0.0.0 Electron/44.10.2"))
    assert desktop["disabled"] is False

    browser = asyncio.run(_run("Mozilla/5.0 Chrome/157.0.0.0 Safari/537.36"))
    assert browser["disabled"] is False
