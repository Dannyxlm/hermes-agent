"""Release pruning and property validation regressions for the unit launcher."""
import shlex
import subprocess

import pytest

from hermes_cli import launch_unit
from tests.hermes_cli.test_launch_unit import cli, rows

pytest_plugins = ["tests.hermes_cli.test_launch_unit"]
pytestmark = pytest.mark.platforms("linux")


@pytest.mark.parametrize("paths", ["pinned", "fallback", "missing"])
def test_finisher_tries_pinned_then_current_under_empty_environment(tmp_path, monkeypatch, paths):
    pinned, fallback = tmp_path / "old release/launch_unit.py", tmp_path / "new release/launch_unit.py"
    output = tmp_path / "receipt"
    for path, label in [(pinned, "pinned"), (fallback, "fallback")]:
        if paths == "pinned" or (paths == "fallback" and path == fallback):
            path.parent.mkdir()
            path.write_text("import pathlib,sys\n" + f"pathlib.Path({str(output)!r}).write_text({label!r} + repr(sys.argv[1:]))\n")
    monkeypatch.setattr(launch_unit, "__file__", str(pinned))
    monkeypatch.setattr(launch_unit, "CURRENT_FINISHER", fallback, raising=False)
    home = tmp_path / "a%b/profile"
    stop = launch_unit._finisher(home, "receipt-id")
    assert "%%" not in stop
    args = shlex.split(stop.removeprefix("ExecStopPost=+"))[4:]
    replacements = {"${SERVICE_RESULT}": "success", "${EXIT_CODE}": "exited", "${EXIT_STATUS}": "0"}
    result = subprocess.run([replacements.get(arg, arg) for arg in args], env={}, cwd=tmp_path,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, (result.stdout, result.stderr)
    if paths == "missing":
        assert not output.exists()
    else:
        assert output.read_text() == paths + repr(["launch", "finish", "--home", str(home), "--launch-id", "receipt-id",
                                                 "--result", "success", "--exit-code", "exited", "--exit-status", "0"])


@pytest.mark.parametrize("prop", ["ExecStart=/bin/true", "ExecStartPre=/bin/true", "ExecStopPost=/bin/true",
                                  "User=root", "Environment=SECRET=no", "Unknown=value", "RemainAfterExit=yes"])
def test_unit_refuses_non_allowlisted_property_before_any_write(launch_env, prop):
    home, argv_file, env = launch_env
    result = cli(env, "unit", "--unit", "refused-property", "-p", prop, "--", "/bin/true")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "unsupported unit property" in result.stderr
    assert rows(home) == [] and not argv_file.exists()


@pytest.mark.parametrize("value,expected", [("1e3", "1000"), ("0.1", "1"), ("2.01", "3")])
def test_unit_timeout_is_normalized_to_integer_seconds(launch_env, value, expected):
    import json
    _, argv_file, env = launch_env
    result = cli(env, "unit", "--unit", "normalized-timeout", "--timeout", value, "--", "/bin/true")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "--property=TimeoutStartSec=" + expected in json.loads(argv_file.read_text())


@pytest.mark.parametrize("prop", ["Type=oneshot", "Type=exec", "Type=simple", "TimeoutStartSec=10", "WorkingDirectory=/",
                                  "Nice=5", "MemoryMax=1G", "MemoryHigh=512M", "CPUQuota=50%", "KillMode=control-group",
                                  "StandardOutput=journal", "StandardError=journal", "RuntimeMaxSec=30"])
def test_unit_accepts_documented_properties(launch_env, prop):
    import json
    _, argv_file, env = launch_env
    result = cli(env, "unit", "--unit", "allowed-property", "-p", prop, "--", "/bin/true")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "--property=" + prop in json.loads(argv_file.read_text())
