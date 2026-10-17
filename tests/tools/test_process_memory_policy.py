"""Interactive gateway background commands never inherit a worker memory cap."""
from unittest.mock import Mock, patch

import pytest

from tools import process_registry as pr


@pytest.mark.platforms("linux")
def test_main_chat_spawn_ignores_legacy_memory_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_LOCAL_MEMORY_MAX_MB", "64")
    monkeypatch.setattr(pr, "_is_supervised_gateway_process", lambda: True)
    monkeypatch.setattr(pr, "_systemd_run_user_scope_available", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")
    registry = pr.ProcessRegistry()
    process = Mock(pid=4321)
    process.stdout = iter([])
    with (patch("subprocess.Popen", return_value=process) as popen,
          patch("threading.Thread"), patch.object(registry, "_write_checkpoint")):
        session = registry.spawn_local("echo main-chat", cwd=str(tmp_path))
    assert "MemoryMax=infinity" in popen.call_args.args[0]
    assert session.systemd_unit.endswith(".scope")
