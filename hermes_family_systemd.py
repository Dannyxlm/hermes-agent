"""Bounded, cached systemd metadata. Call only from Family's blocking snapshot workers."""
import os
import re
import subprocess
import threading
import time

_PROPERTIES = "Id,LoadState,ActiveState,SubState,Result,ExecMainStatus,Description"
_cache = {}
_cache_lock = threading.Lock()


def _read_units(units, env):
    try:
        result = subprocess.run(["systemctl", "show", "--property=" + _PROPERTIES, "--", *units],
                                env=env, capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    signals = {}
    for block in result.stdout.split("\n\n"):
        row = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if row.get("LoadState") == "not-found":
            continue
        unit = row.get("Id")
        if not unit:
            continue
        active = row.get("ActiveState")
        signal = {"command": row.get("Description"), "can_stop": False}
        if active in {"active", "activating", "deactivating"}:
            signal["running"] = True
        elif active == "failed":
            signal.update(running=False, terminal_cause="error")
            try:
                signal["exit_code"] = int(row.get("ExecMainStatus", ""))
            except ValueError:
                pass
        else:
            continue
        signals[unit] = signal
    return signals


def systemd_signals(profiles):
    refs = sorted({(profile, row["backend_ref"]) for profile, snapshot in profiles.items()
                   for row in snapshot.launches if row["kind"] == "process" and row.get("finished_at") is None
                   and isinstance(row.get("backend_ref"), str) and row["backend_ref"].startswith("systemd:")})[:50]
    refs = [(p, ref) for p, ref in refs if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_:@.-]{0,249}", ref[8:])]
    if not refs:
        return {}
    from tools.environments.local import build_subprocess_env
    env = build_subprocess_env()
    path = env.get("PATH", os.defpath)
    units = sorted({ref[8:] for _, ref in refs})
    # Coalesce concurrent phone/desktop polls, including missing/timed-out systemctl results.
    with _cache_lock:
        now = time.monotonic()
        for key in list(_cache):
            if _cache[key][0] <= now:
                del _cache[key]
        missing = [u for u in units if (path, u) not in _cache]
        if missing:
            signals = _read_units(missing, env)
            expires = time.monotonic() + 2
            for unit in missing:
                _cache[(path, unit)] = (expires, signals.get(unit) or signals.get(unit + ".service") or {})
        return {(p, ref): dict(_cache[(path, ref[8:])][1]) for p, ref in refs if _cache[(path, ref[8:])][1]}
