"""Profile defaults are explicit, audited and leave live session pins unchanged."""
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.config import read_user_config_raw
from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport


def rpc(method, **params):
    return server.handle_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


@pytest.fixture
def defaults_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setattr(server, "_served_profile_homes", set())
    (home / "config.yaml").write_text("# preserve me\nagent:\n  reasoning_effort: high\ndelegation:\n  model: ''\n  provider: ''\n")
    transport = SimpleNamespace(auth_identity={"provider": "test", "user_id": "owner"})
    token = bind_transport(transport)
    yield home
    reset_transport(token)


def test_default_reasoning_is_idempotent_audited_and_new_chats_only(defaults_home, monkeypatch):
    agent = SimpleNamespace(reasoning_config={"effort": "high"})
    monkeypatch.setitem(server._sessions, "fixture", {"agent": agent, "create_reasoning_override": {"effort": "high"}})
    response = rpc("config.defaults.set", profile="default", key="reasoning", value="low", confirm=True)
    assert "result" in response, response
    assert response["result"]["changed"] is True
    before = (defaults_home / "config.yaml").stat().st_mtime_ns
    again = rpc("config.defaults.set", profile="default", key="reasoning", value="low", confirm=True)["result"]
    assert again["changed"] is False
    assert (defaults_home / "config.yaml").stat().st_mtime_ns == before
    assert rpc("config.defaults.get", profile="default")["result"]["reasoning"] == "low"
    assert agent.reasoning_config == {"effort": "high"}
    assert "# preserve me" in (defaults_home / "config.yaml").read_text()
    audit = [json.loads(line) for line in (defaults_home / "logs" / "defaults-audit.jsonl").read_text().splitlines()]
    assert [item["status"] for item in audit] == ["intent", "applied"]
    assert audit[-1]["operation_id"] == response["result"]["operation_id"]


def test_delegation_model_writes_and_reads_real_config(defaults_home, monkeypatch):
    from hermes_cli import models_validate
    monkeypatch.setattr(models_validate, "_static_catalog", lambda provider: ["gpt-6.1-sol"])
    result = rpc("config.defaults.set", profile="default", key="delegation", value="gpt-6.1-sol",
                 provider="openai-codex", confirm=True)
    assert "result" in result, result
    cfg = read_user_config_raw(defaults_home / "config.yaml")
    assert cfg["delegation"]["model"] == "gpt-6.1-sol"
    assert cfg["delegation"]["provider"] == "openai-codex"
    assert rpc("config.defaults.get", profile="default")["result"]["delegation_model"] == "gpt-6.1-sol"


def test_defaults_reject_invalid_or_unconfirmed_writes(defaults_home):
    before = (defaults_home / "config.yaml").read_bytes()
    for params in [{"key": "reasoning", "value": "banana", "confirm": True},
                   {"key": "reasoning", "value": "low", "confirm": False},
                   {"key": "delegation", "value": "nonsense", "provider": "not-a-provider", "confirm": True}]:
        assert "error" in rpc("config.defaults.set", profile="default", **params)
    assert (defaults_home / "config.yaml").read_bytes() == before
    assert "error" in rpc("config.defaults.set", key="reasoning", value="low", confirm=True)
    token = bind_transport(SimpleNamespace(auth_identity=None))
    try:
        assert rpc("config.defaults.set", profile="default", key="reasoning", value="low", confirm=True)["error"]["code"] == 4403
    finally:
        reset_transport(token)


def test_secondary_default_write_and_audit_failure_do_not_touch_launch(defaults_home, monkeypatch):
    secondary = defaults_home / "profiles" / "secondary"
    secondary.mkdir(parents=True)
    (secondary / "config.yaml").write_text("agent:\n  reasoning_effort: medium\n")
    launch = (defaults_home / "config.yaml").read_bytes()
    result = rpc("config.defaults.set", profile="secondary", key="reasoning", value="max", confirm=True)
    assert "result" in result, result
    assert rpc("config.defaults.get", profile="secondary")["result"]["reasoning"] == "max"
    assert (defaults_home / "config.yaml").read_bytes() == launch
    from tui_gateway import profile_defaults
    def fail_audit(*args):
        raise OSError("fixture audit unavailable")
    monkeypatch.setattr(profile_defaults, "_audit", fail_audit)
    failed = rpc("config.defaults.set", profile="default", key="reasoning", value="low", confirm=True)
    assert failed["error"]["code"] == 5001
    assert (defaults_home / "config.yaml").read_bytes() == launch


def test_unlisted_delegation_and_internal_identity_cannot_write(defaults_home, monkeypatch):
    from hermes_cli import models_validate
    monkeypatch.setattr(models_validate, "_static_catalog", lambda provider: ["gpt-6.1-sol"])
    before = (defaults_home / "config.yaml").read_bytes()
    result = rpc("config.defaults.set", profile="default", key="delegation", value="gpt-unknown-model",
                 provider="openai-codex", confirm=True)
    assert result["error"]["code"] == 4002
    from hermes_cli.dashboard_auth.ws_tickets import INTERNAL_PROVIDER, INTERNAL_USER_ID
    token = bind_transport(SimpleNamespace(auth_identity={"provider": INTERNAL_PROVIDER, "user_id": INTERNAL_USER_ID}))
    try:
        result = rpc("config.defaults.set", profile="default", key="reasoning", value="low", confirm=True)
        assert result["error"]["code"] == 4403
    finally:
        reset_transport(token)
    assert (defaults_home / "config.yaml").read_bytes() == before


@pytest.mark.parametrize("profile", [".hermes", "hermes", "missing"])
def test_explicit_defaults_target_never_uses_session_alias(defaults_home, profile):
    # Seed both files so a rejected mutation must preserve content AND metadata.
    assert "result" in rpc("config.defaults.set", profile="default", key="reasoning", value="low", confirm=True)
    paths = [defaults_home / "config.yaml", defaults_home / "logs" / "defaults-audit.jsonl"]
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
    response = rpc("config.defaults.set", profile=profile, key="reasoning", value="medium", confirm=True)
    assert response.get("error", {}).get("code") == 4064, response
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths] == before
    if profile in {".hermes", "hermes"}:
        assert rpc("config.defaults.get", profile=profile)["result"]["reasoning"] == "low"


@pytest.mark.platforms("posix")
def test_existing_audit_is_tightened_before_defaults_append(defaults_home):
    audit_path = defaults_home / "logs" / "defaults-audit.jsonl"
    audit_path.parent.mkdir()
    audit_path.write_text('{"status":"existing"}\n')
    audit_path.chmod(0o644)
    response = rpc("config.defaults.set", profile="default", key="reasoning", value="low", confirm=True)
    assert "result" in response, response
    assert stat.S_IMODE(audit_path.stat().st_mode) == 0o600
    records = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert [item["status"] for item in records] == ["existing", "intent", "applied"]
    assert rpc("config.defaults.get", profile="default")["result"]["reasoning"] == "low"
