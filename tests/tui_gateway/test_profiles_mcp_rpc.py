"""Profile MCP JSON-RPC enablement and credential-copy regressions."""

from pathlib import Path

import pytest
import yaml

import tui_gateway.server as server
from hermes_cli.config import read_user_config_raw
from hermes_cli.tools_config import enabled_mcp_server_names
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def _call(method: str, params: dict) -> dict:
    response = server._methods[method]("profiles-mcp", params)
    assert "error" not in response, response.get("error")
    return response["result"]


def _write_config(home: Path, config: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )


def _profile_config(profile_home: Path) -> dict:
    return read_user_config_raw(profile_home / "config.yaml")


def _runtime_enabled_mcp(profile_home: Path) -> set[str]:
    token = set_hermes_home_override(str(profile_home))
    try:
        from hermes_cli.config import load_config

        return enabled_mcp_server_names(load_config())
    finally:
        reset_hermes_home_override(token)


@pytest.fixture()
def profile_homes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    launch_home = tmp_path / ".hermes"
    profile_home = launch_home / "profiles" / "worker"
    launch_home.mkdir()
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    return launch_home, profile_home


def test_profile_mcp_disable_and_reenable_matches_runtime_resolver(profile_homes):
    launch_home, profile_home = profile_homes
    server_entry = {
        "command": "demo-mcp",
        "args": ["--mode", "safe"],
        "enabled": True,
    }
    _write_config(launch_home, {"mcp_servers": {"demo": server_entry}})
    _write_config(
        profile_home,
        {
            "mcp_servers": {
                "demo": {
                    **server_entry,
                    # Legacy state from the original RPC implementation.
                    "disabled": False,
                }
            }
        },
    )

    disabled = _call(
        "profiles.configure",
        {"name": "worker", "enabled_mcp_servers": []},
    )

    assert disabled["applied"]["mcp_servers"] is True
    disabled_entry = _profile_config(profile_home)["mcp_servers"]["demo"]
    assert disabled_entry["enabled"] is False
    assert "disabled" not in disabled_entry
    assert "demo" not in _runtime_enabled_mcp(profile_home)
    described = _call("profiles.describe", {"name": "worker"})
    assert described["mcp_servers"] == [
        {"name": "demo", "enabled": False, "transport": "stdio"}
    ]

    enabled = _call(
        "profiles.configure",
        {"name": "worker", "enabled_mcp_servers": ["demo"]},
    )

    assert enabled["applied"]["mcp_servers"] is True
    enabled_entry = _profile_config(profile_home)["mcp_servers"]["demo"]
    assert enabled_entry["enabled"] is True
    assert "disabled" not in enabled_entry
    assert "demo" in _runtime_enabled_mcp(profile_home)
    described = _call("profiles.describe", {"name": "worker"})
    assert described["mcp_servers"] == [
        {"name": "demo", "enabled": True, "transport": "stdio"}
    ]


def test_profile_mcp_copy_uses_raw_credential_free_projection(
    profile_homes,
    monkeypatch,
):
    launch_home, profile_home = profile_homes
    monkeypatch.setenv("MCP_REF_SECRET", "expanded-reference-value")
    monkeypatch.setenv("MCP_CLIENT_SECRET", "expanded-client-secret")
    launch_entry = {
        "url": (
            "https://literal-user:literal-password@mcp.example.test/api"
            "?token=literal-query-value&signature=literal-signature&region=ca"
            "&client_secret=${MCP_CLIENT_SECRET}"
        ),
        "transport": "http",
        "headers": {
            "Authorization": "Bearer literal-authorization-value",
            "X-API-Key": "${MCP_REF_SECRET}",
            "X-Tenant": "ordinary-tenant",
            "Accept": "application/json",
        },
        "env": {
            "SERVICE_TOKEN": "literal-env-value",
            "REFERENCED_SECRET": "${env:MCP_REF_SECRET}",
            "LOG_LEVEL": "debug",
        },
        "oauth": {
            "client_id": "ordinary-client-id",
            "client_secret": "literal-client-secret",
            "scopes": ["read", "write"],
        },
        "sampling": {"enabled": True, "max_tokens_cap": 1024},
        "timeout": 45,
        "disabled": True,
    }
    _write_config(launch_home, {"mcp_servers": {"secure": launch_entry}})
    _write_config(profile_home, {})

    configured = _call(
        "profiles.configure",
        {"name": "worker", "enabled_mcp_servers": ["secure"]},
    )

    assert configured["applied"]["mcp_servers"] is True
    assert configured["credentials_required"] == {"secure": True}
    copied = _profile_config(profile_home)["mcp_servers"]["secure"]

    assert copied["enabled"] is True
    assert "disabled" not in copied
    assert "Authorization" not in copied["headers"]
    assert copied["headers"] == {
        "X-API-Key": "${MCP_REF_SECRET}",
        "X-Tenant": "ordinary-tenant",
        "Accept": "application/json",
    }
    assert copied["env"] == {
        "REFERENCED_SECRET": "${env:MCP_REF_SECRET}",
        "LOG_LEVEL": "debug",
    }
    assert copied["oauth"] == {
        "client_id": "ordinary-client-id",
        "scopes": ["read", "write"],
    }
    assert copied["sampling"] == {"enabled": True, "max_tokens_cap": 1024}
    assert copied["timeout"] == 45

    assert copied["url"] == (
        "https://mcp.example.test/api?region=ca"
        "&client_secret=${MCP_CLIENT_SECRET}"
    )
    copied_text = repr(copied)
    for literal_secret in (
        "literal-user",
        "literal-password",
        "literal-query-value",
        "literal-signature",
        "literal-authorization-value",
        "literal-env-value",
        "literal-client-secret",
        "expanded-reference-value",
        "expanded-client-secret",
    ):
        assert literal_secret not in copied_text

    # Projection is deep and never mutates the launch profile's source entry.
    assert _profile_config(launch_home)["mcp_servers"]["secure"] == launch_entry
