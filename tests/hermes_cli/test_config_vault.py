"""Vault config registration without growing the general config test module."""

import os
from unittest.mock import patch

import hermes_yaml as yaml


def test_vault_email_codes_is_a_recognized_key_and_off_by_default(tmp_path, capsys):
    """``hermes config set vault.email_codes '<json>'`` used to warn 'not a recognized config key'
    although agent/vault_email_codes.py reads it; the section now lives in DEFAULT_CONFIG, opt-in."""
    import json

    from hermes_cli.config import _validate_config_key, set_config_value
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    section = DEFAULT_CONFIG["vault"]["email_codes"]
    assert section["enabled"] is False and section["inboxes"] == []
    for key in ("vault.email_codes", "vault.email_codes.enabled", "vault.email_codes.inboxes",
                "vault.email_codes.wait_seconds"):
        assert _validate_config_key(key) == (True, None)

    value = json.dumps({"enabled": True, "inboxes": [
        {"addresses": ["someone@example.com"], "gmail_command": ["gws"]}]})
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        set_config_value("vault.email_codes", value)
    out = capsys.readouterr()
    assert "not a recognized config key" not in out.out + out.err
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert saved["vault"]["email_codes"]["enabled"] is True
    assert saved["vault"]["email_codes"]["inboxes"][0]["gmail_command"] == ["gws"]
