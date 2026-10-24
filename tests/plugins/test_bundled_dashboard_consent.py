"""Optional bundled dashboards retain consent checks at mount and request time."""

import json

import pytest

from hermes_cli import web_server_dashboard as dashboard
from hermes_cli import plugins as plugins_mod
from tests.hermes_cli import test_plugin_api_consent_gate as consent

daemon = consent.daemon


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


def test_optional_bundle_refuses_api_assets_and_tab_until_enabled(daemon, tmp_path, monkeypatch):
    bundled_home = tmp_path / "bundled"
    imported = consent._install(bundled_home, "optional-probe")
    manifest = imported.parent / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["default_enabled"] = False
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: bundled_home / "plugins")
    consent._plugins(daemon.launch)
    ping = "/api/plugins/optional-probe/ping"
    asset = "/dashboard-plugins/optional-probe/index.js"

    def visible():
        return {p["name"] for p in daemon.client.get("/api/dashboard/plugins", headers=daemon.owner).json()}

    assert daemon.client.get(ping, headers=daemon.owner).status_code == 404
    assert daemon.client.get(asset).status_code == 404
    assert "optional-probe" not in visible()
    assert not imported.exists()
    consent._plugins(daemon.launch, enabled=["optional-probe"])
    assert daemon.client.get(ping, headers=daemon.owner).json() == {"pong": True}
    assert daemon.client.get(asset).status_code == 200
    assert "optional-probe" in visible()
    consent._plugins(daemon.launch)
    assert daemon.client.get(ping, headers=daemon.owner).status_code == 404
    assert daemon.client.get(asset).status_code == 404
    assert "optional-probe" not in visible()
