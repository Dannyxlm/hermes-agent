"""Bundled official delegation strategies: discover without enabling, select only by config."""

import json
import os
from pathlib import Path
import subprocess
import sys
import types

import hermes_yaml as yaml
import pytest

from hermes_cli import plugins as plugins_mod
from hermes_cli.plugins import PluginManager

REPO = Path(__file__).resolve().parents[2]
KEY = "delegation/advanced-delegation"
OPTION = {"name": "stub", "description": "Offline test provider.", "provider": "custom",
          "base_url": "http://127.0.0.1:9/v1", "model": "stub-model", "api_key": "synthetic-test-key"}


@pytest.fixture
def load(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Real bundled directory and loader; no user plugin, symlink, or enabled-list entry.
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: REPO / "plugins")
    managers = []

    def discover(delegation=None, disabled=()):
        (home / "config.yaml").write_text(yaml.safe_dump({
            "plugins": {"enabled": [], "disabled": list(disabled)},
            "delegation": delegation or {},
        }), encoding="utf-8")
        manager = PluginManager()
        manager.discover_and_load()
        managers.append(manager)
        return home, manager

    yield discover
    for manager in reversed(managers):
        manager.unload()


@pytest.fixture
def children(monkeypatch):
    from tools import delegate_tool as dt
    from tools.delegate_tool_child_run import _SchemaOutcome, _build_result_entry
    built = []

    def build(**kwargs):
        built.append(kwargs)
        return types.SimpleNamespace(tool_progress_callback=None, model=kwargs.get("model"),
                                     provider=kwargs.get("override_provider"),
                                     request_overrides={}, reasoning_config={"effort": "medium"})

    def run(task_index, goal, child=None, **kwargs):
        # Exercise core result assembly, not a made-up route field on a stub result.
        return _build_result_entry(child, {"final_response": "offline stub", "completed": True},
                                   task_index, 0.0, _SchemaOutcome(None, None, [], 0))

    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", build)
    monkeypatch.setattr(dt, "_run_single_child", run)
    return built


def _delegate(task):
    from tools.delegate_tool import delegate_task
    parent = types.SimpleNamespace(session_id="bundled-probe", model="parent-model", provider="custom",
                                   tool_progress_callback=None, _delegate_spinner=None, request_overrides={},
                                   _safe_print=lambda line: None)
    return json.loads(delegate_task(tasks=[task], parent_agent=parent))


def _definition():
    import tools.delegate_tool
    from tools.registry import registry
    return registry.get_definitions({"delegate_task"})[0]["function"]


def test_bundled_registration_and_slash_load_without_selection(load):
    from agent.delegation_registry import get_active_provider
    from hermes_cli.config import load_config_readonly

    home, manager = load()
    assert manager._plugins[KEY].manifest.source == "bundled"
    assert manager._plugins[KEY].enabled
    assert get_active_provider({"backend": "model_map"}).name == "model_map"
    assert get_active_provider({"backend": "profile_routes"}).name == "profile_routes"
    assert get_active_provider(load_config_readonly()["delegation"]).name == "default"
    assert "Delegation mode: Default" in manager._plugin_commands["delegation"]["handler"]("")
    saved = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8-sig"))
    assert saved["plugins"]["enabled"] == [] and saved["delegation"] == {}


@pytest.mark.parametrize("backend", [None, "default"])
def test_default_route_and_schema_unchanged_with_bundle_present(load, children, backend):
    from agent.delegation_registry import get_active_provider

    config = {"model": "pinned-model", "base_url": "http://127.0.0.1:9/v1", "api_key": "synthetic-test-key",
              "backends": {"model_map": {"options": [OPTION]}}}
    if backend:
        config["backend"] = backend
    load(config)
    assert get_active_provider(config).name == "default"
    props = _definition()["parameters"]["properties"]["tasks"]["items"]["properties"]
    assert "option" not in props and "profile" not in props
    out = _delegate({"goal": "unchanged default route"})
    assert out["results"][0]["status"] == "completed"
    assert "route" not in out["results"][0]
    assert children[0]["model"] == "pinned-model" and children[0]["profile_home"] is None


def test_model_map_stub_route_recorded_in_real_result(load, children):
    load({"backend": "model_map", "backends": {"model_map": {"options": [OPTION]}}})
    definition = _definition()
    props = definition["parameters"]["properties"]["tasks"]["items"]["properties"]
    assert props["option"]["enum"] == ["stub"]
    assert "synthetic-test-key" not in json.dumps(definition)
    out = _delegate({"goal": "offline routing proof", "option": "stub"})
    assert out["results"][0]["route"] == {
        "backend": "model_map", "label": "stub", "provider": "custom", "model": "stub-model",
        "reasoning_effort": "medium", "service_tier": "normal",
    }
    assert children[0]["override_api_key"] == "synthetic-test-key"
    assert "synthetic-test-key" not in json.dumps(out)


def test_explicit_disable_prevents_registration(load):
    from agent.delegation_provider import DelegationRouteError
    from agent.delegation_registry import get_active_provider
    _, manager = load(disabled=[KEY])
    assert not manager._plugins[KEY].enabled
    for backend in ("model_map", "profile_routes"):
        with pytest.raises(DelegationRouteError, match="not installed"):
            get_active_provider({"backend": backend})
    assert get_active_provider({}).name == "default"


def test_bundled_cli_show(load):
    home, _ = load()
    env = {**os.environ, "HERMES_HOME": str(home)}
    show = subprocess.run([sys.executable, "-m", "hermes_cli.main", "delegation", "config", "--show"],
                          cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False,
                          stdin=subprocess.DEVNULL)
    assert show.returncode == 0, show.stderr
    assert "Delegation mode: Default" in show.stdout


def test_bundled_enable_still_refuses_managed_dependency_mutation(load, tmp_path, monkeypatch):
    from hermes_cli.plugins_cmd import cmd_enable
    from hermes_cli.plugins_admission import AdmissionRefused
    from pm import paths

    home, _ = load()
    source = tmp_path / "immutable-source"
    source.mkdir()
    (source / ".hermes-release.json").write_text('{"install_mode":"managed-immutable"}', encoding="utf-8")
    monkeypatch.setattr(paths, "repo_root", lambda: source)
    before = (home / "config.yaml").read_bytes()
    with pytest.raises(AdmissionRefused, match="managed-immutable release dependencies are externally owned"):
        cmd_enable(KEY)
    assert (home / "config.yaml").read_bytes() == before
