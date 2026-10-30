"""Unit launcher builds argv; only sudo/systemd are faked, never ledger writes or finishers."""
from contextlib import closing
import json
import os
from pathlib import Path
import pwd
import shlex
import sqlite3
import subprocess
import sys

import pytest

from hermes_state import SessionDB

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.platforms("linux")


@pytest.fixture
def launch_env(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    user = tmp_path / "user"
    user.mkdir()
    fake = tmp_path / "bin"
    fake.mkdir()
    argv_file = tmp_path / "argv.json"
    sudo = fake / "sudo"
    sudo.write_text(f"#!{sys.executable}\n" +
                    "import json,os,sqlite3,sys\nfrom pathlib import Path\n" +
                    f"home=Path(os.environ.get('EXPECTED_LAUNCH_HOME',{str(home)!r}))\n" +
                    "with sqlite3.connect('file:'+str(home/'state.db')+'?mode=ro',uri=True) as db:\n" +
                    " assert db.execute(\"SELECT count(*) FROM work_launches WHERE kind='process'\").fetchone()[0]==1\n" +
                    f"Path({str(argv_file)!r}).write_text(json.dumps(sys.argv[1:]))\n" +
                    "sys.exit(int(os.environ.get('FAKE_SUDO_EXIT','0')))\n")
    sudo.chmod(0o755)
    with SessionDB(home / "state.db") as db:
        db.create_session("launcher", "cli")
    env = dict(PATH=str(fake) + ":/usr/bin:/bin", HOME=str(user), HERMES_HOME=str(home),
               HERMES_STARTED_BY_SESSION_ID="launcher", HERMES_STARTED_BY_PROFILE="default",
               PYTHONPATH=str(ROOT), LANG="C.UTF-8")
    return home, argv_file, env


def cli(env, *args):
    return subprocess.run([sys.executable, "-c", "from hermes_cli.main import main; main()", "launch", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=30, check=False)


def rows(home):
    with closing(sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM work_launches")]


@pytest.mark.parametrize("case,args", [
    ("identity", ["--unit", "valid", "--", "/bin/true"]),
    ("name", ["--unit", "../invalid", "--", "/bin/true"]),
    ("empty", ["--unit", "valid", "--"]),
    ("remain", ["--unit", "valid", "-p", "RemainAfterExit=yes", "--", "/bin/true"]),
    ("type", ["--unit", "valid", "-p", "Type=forking", "--", "/bin/true"]),
])
def test_unit_refuses_before_record_or_spawn(launch_env, case, args):
    home, argv_file, env = launch_env
    if case == "identity":
        env.pop("HERMES_STARTED_BY_SESSION_ID")
    result = cli(env, "unit", *args)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert {"identity": "launcher identity", "name": "unit name", "empty": "empty command",
            "remain": "RemainAfterExit", "type": "Type"}[case] in result.stderr
    assert not argv_file.exists()
    assert rows(home) == []


@pytest.mark.parametrize("root", [False, True])
def test_unit_builds_exact_systemd_command_and_records_first(launch_env, tmp_path, root):
    home, argv_file, env = launch_env
    command = ["/bin/bash", "-c", "printf '%s' untouched", "--odd-arg"]
    result = cli(env, "unit", "--unit", "family-example", "--title", "Example title", "--timeout", "30",
                 "--workdir", str(tmp_path), "-p", "MemoryMax=1G", "--setenv", "EXTRA=a b",
                 *(["--root"] if root else []), "--", *command)
    assert result.returncode == 0, (result.stdout, result.stderr)
    launch, = rows(home)
    assert (launch["kind"], launch["backend_ref"], launch["title"], launch["launcher_session_id"]) == (
        "process", "systemd:family-example", "Example title", "launcher")
    assert "family-example" in result.stdout and launch["launch_id"] in result.stdout
    actual = json.loads(argv_file.read_text())
    stop = next(p for p in actual if p.startswith("ExecStopPost="))
    finisher = shlex.split(stop.removeprefix("ExecStopPost=+"))
    assert finisher[:5] == ["/usr/sbin/runuser", "-u", pwd.getpwuid(os.getuid()).pw_name, "--", sys.executable]
    # An absolute source entry is required: the immutable venv does not install the source package.
    assert Path(finisher[5]).is_absolute()
    assert finisher[6:] == ["launch", "finish", "--home", str(home.resolve()), "--launch-id", launch["launch_id"],
                            "--result", "${SERVICE_RESULT}", "--exit-code", "${EXIT_CODE}", "--exit-status", "${EXIT_STATUS}"]
    assert actual == ["-n", "systemd-run", "--unit=family-example",
                      *([] if root else [f"--uid={os.getuid()}", f"--gid={os.getgid()}"]),
                      "--property=Type=oneshot", "--no-block", "--collect", "--property=TimeoutStartSec=30",
                      f"--working-directory={tmp_path}", "--description=Example title",
                      "--setenv=HOME=" + env["HOME"], "--setenv=PATH=" + env["PATH"],
                      "--setenv=HERMES_STARTED_BY_SESSION_ID=launcher", "--setenv=HERMES_STARTED_BY_PROFILE=default",
                      "--setenv=HERMES_LAUNCH_PARENT_ID=" + launch["launch_id"],
                      "--property=MemoryMax=1G", "--setenv=EXTRA=a b", "--property", stop, "--", *command]


@pytest.mark.parametrize("result,code,status,cause", [
    ("success", "exited", "0", "success"), ("exit-code", "exited", "3", "error"),
    ("success", "killed", "TERM", "user_stop"), ("success", "killed", "INT", "user_stop"),
    ("success", "killed", "KILL", "user_stop"), ("timeout", "killed", "TERM", "error"),
    ("signal", "dumped", "SEGV", "error"),
])
def test_recorded_finisher_works_with_empty_environment(launch_env, tmp_path, result, code, status, cause):
    home, argv_file, env = launch_env
    assert cli(env, "unit", "--unit", "finish-example", "--", "/bin/true").returncode == 0
    actual = json.loads(argv_file.read_text())
    stop = next(p for p in actual if p.startswith("ExecStopPost="))
    finisher = shlex.split(stop.removeprefix("ExecStopPost=+"))
    substitutions = {"${SERVICE_RESULT}": result, "${EXIT_CODE}": code, "${EXIT_STATUS}": status}
    # Exercise the exact unprivileged command runuser executes, not root's privileged prefix.
    argv = [substitutions.get(p, p) for p in finisher[4:]]
    ended = subprocess.run(argv, cwd=tmp_path, env={}, capture_output=True, text=True, timeout=30, check=False)
    assert ended.returncode == 0, (ended.stdout, ended.stderr)
    launch, = rows(home)
    assert launch["terminal_cause"] == cause
    assert launch["finished_at"] is not None


def test_failed_systemd_start_finishes_and_returns_exit_code(launch_env):
    home, argv_file, env = launch_env
    env["FAKE_SUDO_EXIT"] = "7"
    result = cli(env, "unit", "--unit", "failed-example", "--", "/bin/true")
    assert result.returncode == 7, (result.stdout, result.stderr)
    assert argv_file.exists()
    launch, = rows(home)
    assert launch["terminal_cause"] == "failed_start"


def test_finisher_targets_named_launcher_profile_not_execution_home(launch_env, tmp_path):
    home, argv_file, env = launch_env
    launcher = home / "profiles/atlas"
    launcher.mkdir(parents=True)
    with SessionDB(launcher / "state.db") as db:
        db.create_session("launcher", "cli", profile_name="atlas")
    env.update(HERMES_STARTED_BY_PROFILE="atlas", EXPECTED_LAUNCH_HOME=str(launcher))
    result = cli(env, "unit", "--unit", "atlas-example", "--", "/bin/true")
    assert result.returncode == 0, (result.stdout, result.stderr)
    stop = next(p for p in json.loads(argv_file.read_text()) if p.startswith("ExecStopPost="))
    command = shlex.split(stop.removeprefix("ExecStopPost=+"))[4:]
    values = {"${SERVICE_RESULT}": "success", "${EXIT_CODE}": "exited", "${EXIT_STATUS}": "0"}
    ended = subprocess.run([values.get(p, p) for p in command], cwd=tmp_path, env={},
                           capture_output=True, text=True, timeout=30, check=False)
    assert ended.returncode == 0, (ended.stdout, ended.stderr)
    assert rows(home) == []
    launch, = rows(launcher)
    assert launch["launcher_profile"] == "atlas"
    assert launch["terminal_cause"] == "success"
