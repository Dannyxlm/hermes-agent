"""Pinned optional bundles use real discovery and keep their effects behind consent."""

import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys

import hermes_yaml as yaml
import pytest

from hermes_cli import plugins as plugins_mod
from hermes_cli.plugins import PluginManager

REPO = Path(__file__).resolve().parents[2]
BUNDLES = ("draft-cards", "redaction-pack", "profile-picker", "duty-board")


@pytest.fixture
def discover(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: REPO / "plugins")
    from hermes_cli import version_info

    monkeypatch.setattr(version_info, "_cached_version_info", version_info.VersionInfo(
        "0.22.5", "0.22.5", None, "0" * 40, None, "build"))
    managers = []

    def run(enabled=(), disabled=()):
        (home / "config.yaml").write_text(yaml.safe_dump({
            "plugins": {"enabled": list(enabled), "disabled": list(disabled)},
        }), encoding="utf-8")
        manager = PluginManager()
        managers.append(manager)
        manager.discover_and_load()
        return home, manager

    yield run
    for manager in reversed(managers):
        manager.unload()


@pytest.mark.parametrize("name", BUNDLES)
def test_bundle_is_discovered_but_inactive_without_owner_consent(discover, name):
    home, manager = discover()
    assert name in manager._plugins
    plugin = manager._plugins[name]
    assert plugin.manifest.source == "bundled"
    assert not plugin.enabled
    assert not (home / "plugin-data" / name).exists()


@pytest.mark.parametrize("name", BUNDLES)
def test_owner_can_load_bundle_and_explicit_disable_wins(discover, name):
    _, manager = discover(enabled=[name])
    assert name in manager._plugins
    assert manager._plugins[name].enabled, manager._plugins[name].error
    if name == "draft-cards":
        assert {"draft_card", "draft_card_read", "draft_card_status"} <= manager._plugin_tool_names
    if name == "duty-board":
        assert "duty" in manager._plugin_commands
        assert "duty" in manager._cli_commands
    manager.unload()
    _, disabled = discover(enabled=[name], disabled=[name])
    assert not disabled._plugins[name].enabled


def _load_api(name):
    path = REPO / "plugins" / name / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("batch_api_" + name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _no_external_io(monkeypatch):
    calls = []

    def refused(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Plugin attempted network or process execution")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket.socket, "connect_ex", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(subprocess, "Popen", refused)
    return calls


def test_redaction_adds_masks_without_weakening_existing_rules(discover, monkeypatch):
    from agent.redact import redact_sensitive_text

    builtins = ["ghp_" + "aB3x" * 9, "sk-proj-" + "aB3x" * 30]
    original = [redact_sensitive_text(token) for token in builtins]
    assert all(masked != token for masked, token in zip(original, builtins))
    attempts = _no_external_io(monkeypatch)
    home, manager = discover(enabled=["redaction-pack"])
    assert "redaction-pack" in manager._plugins
    assert manager._plugins["redaction-pack"].enabled
    assert [redact_sensitive_text(token) for token in builtins] == original
    token = "shpat_" + "abcdef0123456789" * 2
    assert token not in redact_sensitive_text(token)
    assert not (home / "plugin-data" / "redaction-pack").exists()
    assert attempts == []


def test_drafts_create_revise_approve_and_report_without_sending(discover, monkeypatch):
    from tools.registry import registry

    home, manager = discover(enabled=["draft-cards"])
    assert "draft_card" in manager._plugin_tool_names
    attempts = _no_external_io(monkeypatch)
    create = registry.get_entry("draft_card", scope=manager.scope_key).handler
    read = registry.get_entry("draft_card_read", scope=manager.scope_key).handler
    status = registry.get_entry("draft_card_status", scope=manager.scope_key).handler
    first = json.loads(create({"to": "recipient@example.invalid", "subject": "Synthetic draft", "body": "Version one"}, session_id="synthetic-session"))
    draft_id = first["draft_id"]
    revised = json.loads(create({"draft_id": draft_id, "body": "Version two"}))
    assert revised["version"] == 2
    api = _load_api("draft-cards")
    assert api.approve_draft(draft_id, api.ApproveBody(version=2))["approved_version"] == 2
    approved = json.loads(read({"draft_id": draft_id}))
    assert approved["status"] == "approved" and approved["fields"]["body"] == "Version two"
    assert json.loads(status({"draft_id": draft_id, "status": "sent"}))["status"] == "sent"
    assert (home / "plugin-data/draft-cards/drafts.db").is_file()
    assert attempts == []


def test_duty_database_is_readonly_and_dismissals_persist_only_identifiers(discover, monkeypatch):
    home, manager = discover(enabled=["duty-board"])
    assert "duty" in manager._plugin_commands
    database = home / "state.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE messages (content TEXT)")
        conn.execute("INSERT INTO messages VALUES ('SYNTHETIC_MESSAGE_CONTENT')")
    before = database.read_bytes()
    api = _load_api("duty-board")
    attempts = _no_external_io(monkeypatch)
    with api.board._connect() as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        conn.execute("PRAGMA query_only=0")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO messages VALUES ('forbidden')")
    api.summary()
    assert api.dismiss(api.DismissBody(key="synthetic-session:1")) == {"ok": True}
    saved = json.loads((home / "duty-board/dismissed.json").read_text(encoding="utf-8"))
    assert set(saved) == {"synthetic-session:1"}
    assert isinstance(saved["synthetic-session:1"], (int, float))
    assert database.read_bytes() == before
    assert all(b"SYNTHETIC_MESSAGE_CONTENT" not in path.read_bytes() for path in (home / "duty-board").rglob("*") if path.is_file())
    assert attempts == []
