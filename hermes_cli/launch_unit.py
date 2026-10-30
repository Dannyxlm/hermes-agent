"""Durable system units with explicit, best-effort Family launch receipts."""
import argparse
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from uuid import uuid4


def build_launch_parser(subparsers):
    parser = subparsers.add_parser("launch", help="Launch and finish durable Family work")
    commands = parser.add_subparsers(dest="launch_command", required=True)
    unit = commands.add_parser("unit", help="Start a transient system service linked to this chat")
    unit.add_argument("--unit", required=True)
    unit.add_argument("--title")
    unit.add_argument("--root", action="store_true")
    unit.add_argument("--workdir")
    unit.add_argument("--timeout", type=_timeout)
    unit.add_argument("-p", dest="properties", action="append", default=[])
    unit.add_argument("--setenv", action="append", default=[])
    unit.add_argument("argv", nargs=argparse.REMAINDER)
    unit.set_defaults(func=cmd_unit)
    finish = commands.add_parser("finish", help="Record a unit's terminal receipt")
    finish.add_argument("--home", required=True, type=Path)
    finish.add_argument("--launch-id", required=True)
    finish.add_argument("--result", required=True)
    finish.add_argument("--exit-code", required=True)
    finish.add_argument("--exit-status", required=True)
    finish.set_defaults(func=cmd_finish)


def _timeout(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be positive finite seconds")
    return value


def _validate_unit(args, fields, command):
    if not fields.get("started_by_session") or not fields.get("started_by_profile"):
        raise ValueError("launcher identity is missing; run this command from a bound chat terminal")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_:@.-]{0,249}", args.unit):
        raise ValueError("invalid unit name")
    if not command:
        raise ValueError("empty command; pass -- followed by the command argv")
    for prop in args.properties:
        key, sep, value = prop.partition("=")
        if not sep or key == "RemainAfterExit" or (key == "Type" and value not in {"oneshot", "exec", "simple"}):
            raise ValueError(f"unsupported unit property: {key}")
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", item, re.DOTALL) for item in args.setenv):
        raise ValueError("--setenv requires NAME=VALUE")


def _write(home, method, *args, **kwargs):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_state_launch_context import launch_writer
    from hermes_state_optional_launch import optional_launch
    with optional_launch("unit " + method):
        token = set_hermes_home_override(home)
        try:
            with launch_writer() as writer:
                getattr(writer, method)(*args, **kwargs)
        finally:
            reset_hermes_home_override(token)


def _finisher(home, launch_id):
    import pwd
    # Venvs contain dependencies, not the source package. Pin an absolute release entry as well
    # as its interpreter: ExecStopPost must work from any WorkingDirectory with an empty env.
    argv = ["/usr/sbin/runuser", "-u", pwd.getpwuid(os.getuid()).pw_name, "--",  # windows-footgun: ok — cmd_unit is Linux-only
            os.path.abspath(sys.executable), str(Path(__file__).resolve()), "launch", "finish",
            "--home", str(home), "--launch-id", launch_id,
            "--result", "${SERVICE_RESULT}", "--exit-code", "${EXIT_CODE}", "--exit-status", "${EXIT_STATUS}"]
    return "ExecStopPost=+" + shlex.join(argv).replace("%", "%%")


def _unit_argv(args, fields, home, launch_id, command):
    argv = ["sudo", "-n", "systemd-run", "--unit=" + args.unit]
    if not args.root:
        argv += [f"--uid={os.getuid()}", f"--gid={os.getgid()}"]  # windows-footgun: ok — cmd_unit is Linux-only
    argv += ["--property=Type=oneshot", "--no-block", "--collect",
             "--property=TimeoutStartSec=" + (args.timeout or "infinity"),
             "--working-directory=" + str(Path(args.workdir or os.getcwd()).resolve()),
             "--description=" + (args.title or args.unit),
             "--setenv=HOME=" + os.environ.get("HOME", str(Path.home())),
             "--setenv=PATH=" + os.environ.get("PATH", os.defpath),
             "--setenv=HERMES_STARTED_BY_SESSION_ID=" + fields["started_by_session"],
             "--setenv=HERMES_STARTED_BY_PROFILE=" + fields["started_by_profile"],
             "--setenv=HERMES_LAUNCH_PARENT_ID=" + launch_id]
    argv += ["--property=" + p for p in args.properties]
    argv += ["--setenv=" + item for item in args.setenv]
    return argv + ["--property", _finisher(home, launch_id), "--", *command]


def cmd_unit(args):
    from hermes_cli.launch_provenance import take_launch
    from hermes_cli.profiles import resolve_profile_env
    if sys.platform != "linux":
        print("hermes launch unit requires Linux systemd", file=sys.stderr)
        return 2
    fields = take_launch()
    command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    try:
        _validate_unit(args, fields, command)
        home = Path(resolve_profile_env(fields["started_by_profile"])).absolute()
    except ValueError as exc:
        print(f"hermes launch unit: {exc}", file=sys.stderr)
        return 2
    launch_id = str(uuid4())
    argv = _unit_argv(args, fields, home, launch_id, command)
    _write(home, "record_launch", launch_id, kind="process", launcher_profile=fields["started_by_profile"],
           launcher_session_id=fields["started_by_session"], child_profile=fields["started_by_profile"],
           title=args.title or args.unit, backend_ref="systemd:" + args.unit,
           parent_launch_id=fields.get("parent_launch_id"))
    try:
        code = subprocess.run(argv, timeout=30, check=False).returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"hermes launch unit: {exc}", file=sys.stderr)
        code = 127
    if code:
        _write(home, "finish_launch", launch_id, "failed_start")
    print(f"unit={args.unit} launch_id={launch_id}")
    return code


def cmd_finish(args):
    if not args.home.is_absolute():
        print("hermes launch finish: --home must be absolute", file=sys.stderr)
        return 2
    cause = "error"
    if args.result == "success":
        if args.exit_code == "exited":
            cause = "success"
        elif args.exit_code == "killed" and args.exit_status in {"TERM", "INT", "KILL", "15", "2", "9"}:
            cause = "user_stop"
    _write(args.home, "finish_launch", args.launch_id, cause)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    parser = argparse.ArgumentParser(prog="hermes")
    build_launch_parser(parser.add_subparsers(required=True))
    args = parser.parse_args()
    raise SystemExit(args.func(args))
