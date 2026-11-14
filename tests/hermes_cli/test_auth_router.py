"""Router CLI parsing, saved-row-only refresh, safe output and explicit preference pins."""
import argparse
import json
from types import SimpleNamespace

from agent.account_router.snapshot import read_snapshot, snapshot_path
from hermes_cli import auth_router
from hermes_cli.subcommands.auth import build_auth_parser


def _seed(home):
    rows = [{"id": "a", "label": "Fixture A", "auth_type": "api_key", "access_token": "fixture-only",
             "source": "manual", "priority": 0},
            {"id": "b", "label": "Fixture B", "auth_type": "api_key", "access_token": "fixture-only-other",
             "source": "manual", "priority": 1}]
    (home / "auth.json").write_text(json.dumps({"credential_pool": {"anthropic": rows}}))


def test_cli_refresh_and_json_status_use_saved_credentials_only(tmp_path, monkeypatch, capsys):
    from agent import account_usage, credential_pool
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed(tmp_path)
    def forbidden(*a, **k):
        raise AssertionError("load_pool must never run during meter refresh or status")
    monkeypatch.setattr(credential_pool, "load_pool", forbidden)
    def get(url, headers, *, timeout):
        if headers["Authorization"].endswith("-other"):
            raise ValueError("fixture-body-must-not-be-printed")
        if url.endswith("profile"):
            return {"organization": {"rate_limit_tier": "default_claude_max_5x"}}
        return {"seven_day": {"utilization": 20, "resets_at": 2000000000}}
    monkeypatch.setattr(account_usage, "_get_json", get)
    parser = argparse.ArgumentParser()
    build_auth_parser(parser.add_subparsers(), cmd_auth=auth_router.auth_router_command)
    args = parser.parse_args(["auth", "router", "refresh", "--provider", "anthropic"])
    args.func(args)
    assert capsys.readouterr().out.strip() == "anthropic: updated=1 unknown=1"
    state = read_snapshot(snapshot_path("anthropic"))
    assert "b" not in state["meters"]["anthropic"]
    args = parser.parse_args(["auth", "router", "status", "--provider", "anthropic", "--json"])
    args.func(args)
    output = capsys.readouterr().out
    assert "fixture-only" not in output and "fixture-body" not in output
    rows = json.loads(output)
    assert rows[0]["order"][0]["entry_id"] == "a"
    assert rows[0]["order"][1]["score"] is None


def test_refresh_integrates_codex_and_grok_fetchers(tmp_path, monkeypatch):
    import base64
    from agent import account_usage
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    jwt = "fixture." + base64.urlsafe_b64encode(json.dumps({"sub": "fixture-user"}).encode()).decode().rstrip("=") + ".fixture"
    (tmp_path / "auth.json").write_text(json.dumps({"credential_pool": {
        provider: [{"id": provider, "label": "Fixture account", "source": "manual", "auth_type": "oauth",
                    "access_token": jwt}] for provider in ("openai-codex", "xai-oauth")}}))
    def get(url, headers, *, timeout):
        assert timeout == 5
        if "wham/usage" in url:
            return {"plan_type": "pro", "rate_limit": {"secondary_window": {
                "used_percent": 1, "reset_at": 2000000000, "limit_window_seconds": 604800}}}
        if "/billing" in url:
            return {"config": {"creditUsagePercent": 4, "currentPeriod": {"end": "2033-05-18T03:33:20Z"}}}
        return {"subscriptionTier": "GrokPro"}
    monkeypatch.setattr(account_usage, "_get_json", get)
    assert auth_router.refresh(["openai-codex", "xai-oauth"]) == [
        {"provider": "openai-codex", "updated": 1, "unknown": 0},
        {"provider": "xai-oauth", "updated": 1, "unknown": 0}]
    state = read_snapshot(snapshot_path("openai-codex"))
    assert state["meters"]["openai-codex"]["openai-codex"]["plan"] == "pro"
    assert state["meters"]["xai-oauth"]["xai-oauth"]["plan"] == "GrokPro"
    assert all(row["order"][0]["score"] > 0 for row in auth_router.status(["openai-codex", "xai-oauth"]))


def test_priority_zero_pins_even_existing_primary(tmp_path, monkeypatch):
    from hermes_cli import auth_commands
    from agent.credential_pool import CredentialPool, PooledCredential
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed(tmp_path)
    from hermes_cli.config_backend import write_config_key
    write_config_key(tmp_path / "config.yaml", "credential_pool_strategies.anthropic", "smart")
    pool = CredentialPool("anthropic", [PooledCredential.from_dict("anthropic", r)
                                      for r in json.loads((tmp_path / "auth.json").read_text())["credential_pool"]["anthropic"]])
    monkeypatch.setattr(auth_commands, "load_pool", lambda provider: pool)
    auth_commands.auth_priority_command(SimpleNamespace(provider="anthropic", target="a", priority=0))
    assert read_snapshot(snapshot_path("anthropic"))["pins"]["anthropic"]["entry_id"] == "a"
