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
    assert state["meters"]["anthropic"]["b"]["windows"] == []
    args = parser.parse_args(["auth", "router", "status", "--provider", "anthropic", "--json"])
    args.func(args)
    output = capsys.readouterr().out
    assert "fixture-only" not in output and "fixture-body" not in output
    rows = json.loads(output)
    assert rows[0]["order"][0]["entry_id"] == "a"
    assert rows[0]["order"][1]["score"] is None


def test_priority_zero_pins_even_existing_primary(tmp_path, monkeypatch):
    from hermes_cli import auth_commands
    from agent.credential_pool import CredentialPool, PooledCredential
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed(tmp_path)
    pool = CredentialPool("anthropic", [PooledCredential.from_dict("anthropic", r)
                                      for r in json.loads((tmp_path / "auth.json").read_text())["credential_pool"]["anthropic"]])
    monkeypatch.setattr(auth_commands, "load_pool", lambda provider: pool)
    auth_commands.auth_priority_command(SimpleNamespace(provider="anthropic", target="a", priority=0))
    assert read_snapshot(snapshot_path("anthropic"))["pins"]["anthropic"]["entry_id"] == "a"
