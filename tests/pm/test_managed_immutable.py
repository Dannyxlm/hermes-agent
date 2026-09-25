"""Externally released Python cannot be replaced by PM application generations."""
import json
import os
from pathlib import Path
import sys

import pytest

from pm import client, environments, install, paths
from pm.package import InstallError


@pytest.mark.parametrize("managed", [False, True])
def test_release_ownership_controls_boot_selection(tmp_path, monkeypatch, managed):
    root = tmp_path / "release"
    root.mkdir()
    (root / ".hermes-release.json").write_text(json.dumps({
        "install_mode": "managed-immutable" if managed else "external",
    }), encoding="utf-8")
    generation = environments.install_state_dir(root) / "environments" / "old" / "venv"
    generation.mkdir(parents=True)
    (generation / "pyvenv.cfg").write_text("version = 3.11.15\n", encoding="utf-8")
    facts = environments.runtime_facts_path(root)
    facts.write_text(json.dumps({"packages": {"venv": {"environment": str(generation)}}}), encoding="utf-8")
    before = facts.read_bytes()
    if managed:
        from hermes_cli._early_recovery import recover_if_needed

        (root / "pyproject.toml").write_text('[project]\nname="hermes-test"\n', encoding="utf-8")
        marker = root / ".update-incomplete"
        marker.write_text('{"attempts":0}', encoding="utf-8")
        assert recover_if_needed(root, argv=["gateway"]) is False
        assert marker.read_text(encoding="utf-8") == '{"attempts":0}'
        original_path, original_env = sys.path[:], dict(os.environ)
        environments.activate_dependencies(root)
        assert sys.path == original_path
        assert dict(os.environ) == original_env
        assert environments.committed_venv(root) is None
        assert environments.selected_venv(root) == Path(sys.prefix)
    else:
        assert environments.committed_venv(root) == generation
        assert environments.selected_venv(root) == generation
    assert facts.read_bytes() == before


@pytest.mark.parametrize("route", ["client-sync", "client-repair", "client-ensure", "worker-sync", "worker-repair", "cli-repair"])
def test_managed_release_refuses_application_mutation_before_install(tmp_path, monkeypatch, capsys, route):
    root = tmp_path / "release"
    root.mkdir()
    (root / ".hermes-release.json").write_text(
        '{"install_mode":"managed-immutable"}', encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname=\"hermes-test\"\n", encoding="utf-8")
    monkeypatch.setattr(paths, "repo_root", lambda: root)
    monkeypatch.setattr(client, "_worker_command", lambda *a: pytest.fail("bootstrapped PM"))
    monkeypatch.setattr(install, "_feature_policy", lambda *a, **kw: pytest.fail("entered dependency resolution"))
    if route == "client-ensure":
        action = lambda: client.ensure("venv", explicit=True)
    else:
        operation = client.sync_venv if route.startswith("client") else install.sync_venv
        action = lambda: operation(explicit=True, repair=route.endswith("repair"))
    if route == "cli-repair":
        from pm.cli import cmd_repair

        assert cmd_repair(None) == 1
        assert "managed-immutable" in capsys.readouterr().err
    else:
        with pytest.raises(InstallError, match="managed-immutable"):
            action()
    assert not environments.install_state_dir(root).exists()
    assert not paths.store_root().exists()
    assert {p.name for p in root.iterdir()} == {".hermes-release.json", "pyproject.toml"}
