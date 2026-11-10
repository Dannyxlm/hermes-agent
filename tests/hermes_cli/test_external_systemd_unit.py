"""System service operations must not adopt externally authored units."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_cli import gateway as gw
from hermes_cli import _launchers

pytestmark = pytest.mark.platforms("linux")


@pytest.fixture
def service(tmp_path, monkeypatch):
    unit = tmp_path / "hermes-gateway.service"
    monkeypatch.setattr(gw, "get_systemd_unit_path", lambda system=False: unit)
    monkeypatch.setattr(gw, "_require_root_for_system_service", lambda action: None)
    monkeypatch.setattr(gw.gateway_service_owner, "unit_belongs_to_caller", lambda *a, **kw: True)
    monkeypatch.setattr(gw.gateway_service_owner, "refuse_foreign_systemd_unit", lambda *a: None)
    monkeypatch.setattr(gw.gateway_service_owner, "refuse_temp_home_service_write", lambda *a: False)
    monkeypatch.setattr(gw, "_read_systemd_user_from_unit", lambda path: "service")
    monkeypatch.setattr(gw, "_system_service_identity", lambda user=None: ("service", "service", str(tmp_path), 1000))
    monkeypatch.setattr(gw, "_sync_hermes_home_from_systemd_unit", lambda **kw: None)
    monkeypatch.setattr(gw, "_prepare_service_launcher", Mock())
    monkeypatch.setattr(gw, "_run_systemctl", Mock())
    monkeypatch.setattr(gw, "has_legacy_hermes_units", lambda: False)
    monkeypatch.setattr(gw, "_ensure_system_service_linger", lambda user: None)
    monkeypatch.setattr(gw, "print_systemd_scope_conflict_warning", lambda: None)
    monkeypatch.setattr(gw, "print_legacy_unit_warning", lambda: None)
    return unit


@pytest.mark.parametrize("operation", ["start", "restart", "install", "force_install"])
def test_external_unit_survives_operations_byte_for_byte(service, monkeypatch, capsys, operation):
    original = (f'[Unit]\nDescription={gw.SERVICE_DESCRIPTION}\n[Service]\n'
                'ExecStart=/srv/service-wrapper gateway run\nEnvironment="HERMES_HOME=/srv/state"\n').encode()
    service.write_bytes(original)
    monkeypatch.setattr(gw, "generate_systemd_unit", lambda **kw: "replacement")
    monkeypatch.setattr(gw, "_systemd_main_pid", lambda **kw: None)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr(gw, "_recover_pending_systemd_restart", lambda **kw: False)
    monkeypatch.setattr(gw, "_wait_for_systemd_service_restart", lambda **kw: True)
    if operation.endswith("install"):
        gw.systemd_install(system=True, force=operation == "force_install", force_unit_path=True)
    else:
        getattr(gw, f"systemd_{operation}")(system=True)
    assert service.read_bytes() == original
    gw._prepare_service_launcher.assert_not_called()
    calls = [call.args[0] for call in gw._run_systemctl.call_args_list]
    assert ["daemon-reload"] not in calls
    assert (["start", gw.get_service_name()] if operation == "start" else
            ["restart", gw.get_service_name()] if operation == "restart" else
            ["enable", gw.get_service_name()]) in calls
    assert capsys.readouterr().out.count("externally managed") == 1


@pytest.mark.parametrize("source_launcher", [False, True])
def test_generated_outdated_unit_still_refreshes(service, monkeypatch, source_launcher):
    if source_launcher:
        monkeypatch.setattr(_launchers, "resolve_store_python", lambda *a, **kw: Path("/store/bin/python"))
    else:
        monkeypatch.setattr(_launchers, "resolve_store_python", lambda *a, **kw: None)
    current = gw.generate_systemd_unit(system=True, run_as_user="service")
    service.write_text(current.replace("RestartSec=5", "RestartSec=9"))
    assert gw.refresh_systemd_unit_if_needed(system=True)
    assert service.read_text() == current
    gw._prepare_service_launcher.assert_called_once_with(system=True, run_as_user="service")


def test_immutable_launcher_publication_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "release"
    root.mkdir()
    (root / ".hermes-release.json").write_text('{"install_mode":"managed-immutable"}')
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda *a, **kw: Path("/store/bin/python"))
    with pytest.raises(RuntimeError, match="externally owned"):
        _launchers.ensure_install_launchers(root, root / ".hermes" / "bin")
    assert not (root / ".hermes").exists()


def test_immutable_cli_exposure_does_not_publish(tmp_path, monkeypatch):
    (tmp_path / ".hermes-release.json").write_text('{"install_mode":"managed-immutable"}')
    publish = Mock(side_effect=AssertionError("publication must not run"))
    monkeypatch.setattr(_launchers, "ensure_install_launchers", publish)
    assert _launchers.expose_cli(tmp_path) == {"ok": True, "skipped": "externally-owned"}
    publish.assert_not_called()
