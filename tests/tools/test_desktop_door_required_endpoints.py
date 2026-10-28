"""Endpoint omissions fail before credential access or a loopback connection."""
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture(params=[{}, {"port": 8081}, {"viewer_base_url": "https://viewer.example.test/desktop"}])
def incomplete_door(request, tmp_path, monkeypatch):
    from hermes_cli.config import atomic_config_write
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: tmp_path)
    credential = tmp_path / "synthetic-credential"
    credential.write_text("synthetic-token")
    atomic_config_write(tmp_path / "config.yaml", {"cloudseed_mobile": {"desktop_door": {
        "credential_file": str(credential), **request.param,
    }}})
    original_read = Path.read_text
    credential_read = Mock(side_effect=AssertionError("must not read the bearer credential"))

    def read_text(path, *args, **kwargs):
        if path == credential:
            return credential_read()
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    connection = Mock(side_effect=AssertionError("must not open HTTPConnection"))
    monkeypatch.setattr("http.client.HTTPConnection", connection)
    yield request.param
    credential_read.assert_not_called()
    connection.assert_not_called()


def test_missing_endpoint_settings_fail_before_io(incomplete_door):
    from tools.desktop_door_client import DoorClient, DoorNotConfigured, DoorUnavailable

    with pytest.raises(DoorUnavailable) as failure:
        DoorClient.configured()
    # A partial configuration must not take the optional-feature bypass.
    assert not isinstance(failure.value, DoorNotConfigured)
    for key in {"port", "viewer_base_url"} - incomplete_door.keys():
        assert f"cloudseed_mobile.desktop_door.{key}" in str(failure.value)


def test_partial_configuration_blocks_only_desktop_admission(incomplete_door):
    from tools.desktop_admission import DesktopInputDenied, assert_agent_admitted

    assert assert_agent_admitted("http://localhost:9222") is None
    with pytest.raises(DesktopInputDenied, match="Desktop admission unavailable"):
        assert_agent_admitted("http://localhost:9225")
