"""Optional bundled dashboards retain consent checks at mount and request time."""

import json

import pytest

from hermes_cli import web_server_dashboard as dashboard


@pytest.mark.parametrize("enabled,disabled,allowed", [
    ([], [], False), (["optional-dashboard"], [], True),
    (["optional-dashboard"], ["optional-dashboard"], False),
])
def test_bundled_dashboard_can_require_explicit_consent(tmp_path, enabled, disabled, allowed):
    directory = tmp_path / "optional-dashboard" / "dashboard"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps({
        "name": "optional-dashboard", "api": "plugin_api.py", "default_enabled": False,
    }), encoding="utf-8")
    (directory / "plugin_api.py").write_text("from fastapi import APIRouter\nrouter = APIRouter()\n", encoding="utf-8")
    plugins = dashboard._discover_dashboard_plugins([(tmp_path, "bundled")])
    assert len(plugins) == 1
    reason = dashboard._plugin_api_mount_skip_reason(plugins[0], set(enabled), set(disabled))
    assert (reason is None) is allowed


def test_existing_bundled_dashboard_default_is_preserved():
    plugin = {"name": "existing", "source": "bundled"}
    assert dashboard._plugin_api_mount_skip_reason(plugin, set(), set()) is None
    assert dashboard._plugin_api_mount_skip_reason(plugin, set(), {"existing"}) is not None
