"""Profile defaults are explicit, audited and leave live session pins unchanged."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli.config import read_user_config_raw
from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport


def rpc(method, **params):
    return server.handle_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


@pytest.fixture
def defaults_home(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_profile_home", lambda p: None if p == "default" else tmp_path / p)
    (tmp_path / "config.yaml").write_text("# preserve me\nagent:\n  reasoning_effort: high\ndelegation:\n  model: ''\n  provider: ''\n")
    transport = SimpleNamespace(auth_identity={"provider": "test", "user_id": "owner"})
    token = bind_transport(transport)
    yield tmp_path
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
    secondary = defaults_home / "secondary"
    secondary.mkdir()
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
