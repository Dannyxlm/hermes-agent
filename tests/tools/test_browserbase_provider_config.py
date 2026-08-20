from __future__ import annotations

from unittest.mock import Mock, patch

from plugins.browser.browserbase.provider import BrowserbaseBrowserProvider


def _created_response() -> Mock:
    response = Mock()
    response.ok = True
    response.status_code = 201
    response.json.return_value = {
        "id": "bb-session-123",
        "connectUrl": "wss://connect.browserbase.test/session",
    }
    return response


def test_config_disables_proxies_and_attaches_opted_in_session_metadata(monkeypatch):
    monkeypatch.setenv("BROWSERBASE_API_KEY", "test-api-key")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "test-project")
    monkeypatch.setenv("BROWSERBASE_PROXIES", "true")

    provider = BrowserbaseBrowserProvider()
    response = _created_response()
    config = {
        "browser": {
            "browserbase": {
                "proxies": False,
                "session_metadata": True,
            }
        }
    }

    with (
        patch("hermes_cli.config.read_raw_config", return_value=config),
        patch("plugins.browser.browserbase.provider.requests.post", return_value=response) as post,
    ):
        session = provider.create_session("telegram-session-abc")

    payload = post.call_args.kwargs["json"]
    assert "proxies" not in payload
    assert payload["userMetadata"] == {
        "source": "hermes",
        "provider": "browserbase",
        "task_id": "telegram-session-abc",
        "proxies": "false",
    }
    features = session["features"]
    assert isinstance(features, dict)
    assert features["proxies"] is False


def test_config_can_explicitly_enable_proxies(monkeypatch):
    monkeypatch.setenv("BROWSERBASE_API_KEY", "test-api-key")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "test-project")
    monkeypatch.delenv("BROWSERBASE_PROXIES", raising=False)

    provider = BrowserbaseBrowserProvider()
    response = _created_response()
    config = {"browser": {"browserbase": {"proxies": True}}}

    with (
        patch("hermes_cli.config.read_raw_config", return_value=config),
        patch("plugins.browser.browserbase.provider.requests.post", return_value=response) as post,
    ):
        session = provider.create_session("task-123")

    payload = post.call_args.kwargs["json"]
    assert payload["proxies"] is True
    assert "userMetadata" not in payload
    features = session["features"]
    assert isinstance(features, dict)
    assert features["proxies"] is True


def test_legacy_proxy_env_is_used_only_when_config_key_is_absent(monkeypatch):
    monkeypatch.setenv("BROWSERBASE_API_KEY", "test-api-key")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "test-project")
    monkeypatch.setenv("BROWSERBASE_PROXIES", "false")

    provider = BrowserbaseBrowserProvider()

    with (
        patch("hermes_cli.config.read_raw_config", return_value={}),
        patch(
            "plugins.browser.browserbase.provider.requests.post",
            return_value=_created_response(),
        ) as post,
    ):
        session = provider.create_session("legacy-env")

    payload = post.call_args.kwargs["json"]
    assert "proxies" not in payload
    assert "userMetadata" not in payload
    features = session["features"]
    assert isinstance(features, dict)
    assert features["proxies"] is False


def test_proxy_fallback_metadata_reports_effective_state(monkeypatch):
    monkeypatch.setenv("BROWSERBASE_API_KEY", "test-api-key")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "test-project")

    provider = BrowserbaseBrowserProvider()
    payment_required = Mock(ok=False, status_code=402)
    config = {
        "browser": {
            "browserbase": {
                "proxies": True,
                "session_metadata": True,
            }
        }
    }

    with (
        patch("hermes_cli.config.read_raw_config", return_value=config),
        patch(
            "plugins.browser.browserbase.provider.requests.post",
            side_effect=[payment_required, payment_required, _created_response()],
        ) as post,
    ):
        session = provider.create_session("task-fallback")

    retry_payload = post.call_args_list[-1].kwargs["json"]
    assert retry_payload["userMetadata"]["proxies"] == "false"
    features = session["features"]
    assert isinstance(features, dict)
    assert features["proxies"] is False
