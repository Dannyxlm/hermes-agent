"""Only the registered Hermex callback enters the native PKCE broker."""

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider, native_flow
from hermes_cli.dashboard_auth.routes import _validate_native_redirect_uri
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider


CALLBACK = "com.cloudseed.hermex:/oauth/callback"


@pytest.mark.parametrize("callback", [
    "com.cloudseed.hermex:/oauth/callback/", "com.cloudseed.hermex://oauth/callback",
    "com.cloudseed.hermex:/oauth/callback?extra=1", "com.cloudseed.hermex:/oauth/callback#fragment",
    "com.cloudseed.hermex:/oauth/callback?", "com.cloudseed.hermex:/oauth/callback#",
    "com.cloudseed.hermex:/oauth/Callback", "com.cloudseed.hermex:/oauth/%63allback",
    "com.cloudseed.hermex://user@oauth/callback", "com.cloudseed.hermex.evil:/oauth/callback",
    "COM.CLOUDSEED.HERMEX:/oauth/callback", " com.cloudseed.hermex:/oauth/callback",
    "https://example.invalid/oauth/callback", "com.cloudseed.hermex:/oauth/callback\n",
])
def test_mobile_callback_lookalikes_rejected(callback):
    with pytest.raises(HTTPException):
        _validate_native_redirect_uri(callback)


@pytest.mark.parametrize("callback", [CALLBACK, "http://127.0.0.1:4444/callback", "http://[::1]:4444/callback"])
def test_exact_callback_and_desktop_loopback_preserved(callback):
    assert _validate_native_redirect_uri(callback) == callback


@pytest.fixture
def client():
    native_flow._reset_for_tests()
    previous = {key: getattr(web_server.app.state, key, None) for key in ("auth_required", "bound_host", "bound_port")}
    clear_providers()
    register_provider(StubAuthProvider())
    web_server.app.state.auth_required = True
    web_server.app.state.bound_host = "mobile.example.test"
    web_server.app.state.bound_port = 443
    test_client = TestClient(web_server.app, base_url="https://mobile.example.test", follow_redirects=False)
    yield test_client
    test_client.close()
    clear_providers()
    native_flow._reset_for_tests()
    for key, value in previous.items():
        setattr(web_server.app.state, key, value)


def pkce():
    verifier = "mobile-fixture-verifier-material-0123456789abcdefgh"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize(client, callback=CALLBACK):
    verifier, challenge = pkce()
    result = client.get("/auth/native/authorize", params={"provider": "stub", "redirect_uri": callback,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "mobile-client-state"})
    return verifier, result


def test_mobile_browser_to_pkce_bearer_to_ticket(client):
    verifier, start = authorize(client)
    assert start.status_code == 302
    query = parse_qs(urlparse(start.headers["location"]).query)
    callback = client.get("/auth/callback", params={"code": query["code"][0], "state": query["state"][0]})
    assert callback.status_code == 302
    location = callback.headers["location"]
    assert location.split("?", 1)[0] == CALLBACK
    assert "hermes_session_at" not in callback.headers.get("set-cookie", "")
    returned = parse_qs(urlparse(location).query)
    assert returned["state"] == ["mobile-client-state"]
    code = returned["code"][0]
    tokens = client.post("/auth/native/token", json={"code": code, "code_verifier": verifier})
    assert tokens.status_code == 200
    assert client.post("/auth/native/token", json={"code": code, "code_verifier": verifier}).status_code == 400
    ticket = client.post("/api/auth/ws-ticket", headers={"Authorization": f"Bearer {tokens.json()['access_token']}"})
    assert ticket.status_code == 200
    assert ticket.json()["ticket"]


def test_invalid_mobile_callback_rejected_before_broker_registration(client, monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError("invalid callback reached broker registration")
    monkeypatch.setattr(native_flow, "register_pending", forbidden)
    _, response = authorize(client, CALLBACK + "?destination=other")
    assert response.status_code == 400


def test_mobile_pkce_failure_consumes_code(client):
    verifier, start = authorize(client)
    query = parse_qs(urlparse(start.headers["location"]).query)
    callback = client.get("/auth/callback", params={"code": query["code"][0], "state": query["state"][0]})
    code = parse_qs(urlparse(callback.headers["location"]).query)["code"][0]
    assert client.post("/auth/native/token", json={"code": code, "code_verifier": "wrong"}).status_code == 400
    assert client.post("/auth/native/token", json={"code": code, "code_verifier": verifier}).status_code == 400
