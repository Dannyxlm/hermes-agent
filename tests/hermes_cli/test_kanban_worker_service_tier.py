"""Provider-scoped worker tiers cross the real spawn/config boundary."""
from types import SimpleNamespace

import hermes_yaml as yaml
import pytest


def _spawn(monkeypatch, tmp_path, *, provider="openai-codex", config=None):
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
    from hermes_cli import profiles

    home = tmp_path / "worker"
    home.mkdir()
    home.joinpath("config.yaml").write_text(yaml.safe_dump(config or {
        "model": {"provider": "anthropic", "default": "claude-opus-5-5"},
        "kanban": {"service_tier_by_provider": {"openai-codex": "fast"}},
    }))
    monkeypatch.setattr(profiles, "resolve_profile_env", lambda _: str(home))
    monkeypatch.setattr(dispatch, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(dispatch, "_restart_safe_worker_argv", lambda task, cmd, resources=None: cmd)
    monkeypatch.setattr(dispatch, "_retag_legacy_worker_sessions", lambda _: None)
    captured = {}

    def popen(cmd, **kwargs):
        captured.update(kwargs["env"])
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(dispatch.subprocess, "Popen", popen)
    task = kb.Task(
        id="t_tier", title="tier", body=None, assignee="default", status="running",
        priority=0, created_by="test", created_at=1, started_at=None,
        completed_at=None, workspace_kind="dir", workspace_path=None,
        claim_lock="lock", claim_expires=None, tenant=None,
        model_override="gpt-6-astra" if provider else None, provider_override=provider,
    )
    dispatch._default_spawn(task, str(tmp_path))
    return home, captured


def _load_worker(monkeypatch, home, env):
    import cli
    from hermes_cli.cli_config_load import load_cli_config, _parse_service_tier_config

    monkeypatch.setattr(cli, "_hermes_home", home)
    for key in ("HERMES_KANBAN_TASK", "_HERMES_KANBAN_SERVICE_TIER"):
        monkeypatch.delenv(key, raising=False)
        if key in env:
            monkeypatch.setenv(key, env[key])
    config = load_cli_config()
    return _parse_service_tier_config(config["agent"]["service_tier"])


@pytest.mark.parametrize("tier, expected", [("normal", None), ("ultrafast", "ultrafast")])
def test_explicit_global_tier_still_wins(monkeypatch, tmp_path, tier, expected):
    home, env = _spawn(monkeypatch, tmp_path, config={
        "agent": {"service_tier": tier},
        "kanban": {"service_tier_by_provider": {"openai-codex": "fast"}},
    })
    assert _load_worker(monkeypatch, home, env) == expected


@pytest.mark.parametrize("provider", ["anthropic", "xai", None])
def test_unmapped_workers_clear_inherited_tier(monkeypatch, tmp_path, provider):
    from hermes_cli.kanban_worker_tier import set_worker_service_tier
    home, env = _spawn(monkeypatch, tmp_path, provider=provider)
    env["_HERMES_KANBAN_SERVICE_TIER"] = "fast"
    set_worker_service_tier(env, SimpleNamespace(provider_override=provider), str(home))
    assert "_HERMES_KANBAN_SERVICE_TIER" not in env
    assert _load_worker(monkeypatch, home, env) is None


@pytest.mark.parametrize("mapping", ["fast", ["openai-codex"], 7,
    {"openai-codex": "typo"}, {"openai-codex": True}, {"openai-codex": None}])
def test_invalid_policy_does_not_change_worker(monkeypatch, tmp_path, mapping):
    home, env = _spawn(monkeypatch, tmp_path, config={
        "kanban": {"service_tier_by_provider": mapping},
    })
    assert "_HERMES_KANBAN_SERVICE_TIER" not in env
    assert _load_worker(monkeypatch, home, env) is None


@pytest.mark.parametrize("context", ["interactive", "delegated"])
def test_nonworker_does_not_consume_tier(monkeypatch, tmp_path, context):
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
    home, env = _spawn(monkeypatch, tmp_path)
    if context == "interactive":
        env.pop("HERMES_KANBAN_TASK")
    else:
        monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, str(tmp_path))
    assert _load_worker(monkeypatch, home, env) is None


def test_unconfigured_policy_defaults_to_empty(monkeypatch, tmp_path):
    from hermes_cli.config import load_config_readonly
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert load_config_readonly()["kanban"]["service_tier_by_provider"] == {}


def test_codex_pinned_worker_receives_priority(monkeypatch, tmp_path):
    home, env = _spawn(monkeypatch, tmp_path)
    tier = _load_worker(monkeypatch, home, env)
    assert tier == "priority"
    from cli import HermesCLI
    from agent.codex_runtime import _codex_turn_service_tier
    for mode in ("codex_responses", "codex_app_server"):
        worker = SimpleNamespace(
            service_tier=tier, model="gpt-6-astra", provider="openai-codex",
            api_key="test", base_url="https://chatgpt.com/backend-api/codex",
            api_mode=mode, acp_command=None, acp_args=[],
        )
        route = HermesCLI._resolve_turn_agent_config(worker, "hello")
        assert route["request_overrides"] == {"service_tier": "priority"}
        worker.request_overrides = route["request_overrides"]
        assert _codex_turn_service_tier(worker) == "fast"


@pytest.mark.parametrize("model", [
    {"provider": "openai-codex", "default": "gpt-6-astra"},
    {"provider": "auto", "default": {"provider": "openai-codex", "model": "gpt-6-astra"}},
])
def test_unpinned_worker_uses_assigned_profile_provider(monkeypatch, tmp_path, model):
    home, env = _spawn(monkeypatch, tmp_path, provider=None, config={
        "model": model,
        "kanban": {"service_tier_by_provider": {"openai-codex": "fast"}},
    })
    assert _load_worker(monkeypatch, home, env) == "priority"
