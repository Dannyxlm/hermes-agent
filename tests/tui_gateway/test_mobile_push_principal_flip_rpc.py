"""A derivation upgrade on the same authenticated socket must not duplicate an APNs event."""

from tests.tui_gateway.test_methods_mobile import mobile_home as mobile_home, peer as peer, open_bot, rpc  # noqa: PLC0414
from tests.tui_gateway.test_methods_mobile_push import push_service as push_service, registration  # noqa: PLC0414
from tui_gateway import methods_browser_control, server


def test_dispatcher_principal_upgrade_replaces_device_without_broadening_ownership(push_service, peer, monkeypatch):
    peer.auth_identity = {"user_id": "test-user", "provider": "test-auth"}
    opened = open_bot()
    params = registration(opened)
    monkeypatch.setattr(methods_browser_control, "_principal_digest", lambda _: "old-owner")
    assert "result" in rpc("mobile.push.register", **params)
    monkeypatch.setattr(methods_browser_control, "_principal_digest", lambda _: "current-owner")
    assert "result" in rpc("mobile.push.register", **params)
    sid = opened["session_id"]
    server._emit("message.start", sid)
    server._emit("message.complete", sid, {"status": "complete"})
    push_service.drain_once()
    assert len(push_service.sender.jobs) == 1
    assert push_service.sender.jobs[0]["principal"] == "current-owner"
    ids = {key: params[key] for key in ("installation_id", "connection_id")}
    monkeypatch.setattr(methods_browser_control, "_principal_digest", lambda _: "old-owner")
    assert rpc("mobile.push.unregister", **ids)["result"]["removed"] == 0
    monkeypatch.setattr(methods_browser_control, "_principal_digest", lambda _: "current-owner")
    assert rpc("mobile.push.unregister", **ids)["result"]["removed"] == 1
