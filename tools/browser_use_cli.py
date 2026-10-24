"""Use the Browser Use CLI 3.0 (https://browser-use.com) for browser automation

When browser.backend is "browser-use", the model gets ``browser_exec`` tool
instead of default browser tools
"""

import contextlib
import functools
import importlib
import importlib.util
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from hermes_constants import get_hermes_home, hermes_home_key
from tools.browser_harness_dirs import daemon_env, daemon_identity, namespace_harness_dirs
from utils import is_truthy_value

logger = logging.getLogger(__name__)

_BACKEND_KEY = "browser-use"
BACKEND_DISABLED = "off"

# Cloud daemon names become the BU_NAME env var
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Set on the env dict by the CDP resolvers when the resolved browser is EXCLUSIVE to this named session
# (per-name provider / named BU cloud / Lightpanda). Popped before the subprocess launches — never exported.
_PRIVATE_BROWSER_SENTINEL = "_HERMES_BU_PRIVATE_BROWSER"
# Internal route provenance: this exec resolved to a browser on the Bot Desktop display and must use
# the same human-control lease fence as the built-in browser tools. Popped before launching the CLI.
_BOT_DESKTOP_BROWSER_SENTINEL = "_HERMES_BU_BOT_DESKTOP_BROWSER"
# Internal route provenance: this exec resolved to a browser OTHER tasks also drive (operator BU_CDP_*,
# /browser connect / browser.cdp_url, the real-profile browser). Popped before launching the CLI.
_SHARED_BROWSER_SENTINEL = "_HERMES_BU_SHARED_BROWSER"
# The note when the consented real profile could not load (the route fell back). Popped before launch.
_REAL_PROFILE_FALLBACK_SENTINEL = "_HERMES_BU_REAL_PROFILE_FALLBACK"
# local=True whose real profile could not load: run on the local sidecar browser. Popped before launch.
_LOCAL_FALLBACK_SENTINEL = "_HERMES_BU_LOCAL_FALLBACK"

# Named sessions on SHARED browsers need no Hermes-side tab pinning: browser-harness (>=0.1.13, pinned in
# pyproject) gives every non-default daemon its own dedicated BACKGROUND tab and closes it on shutdown.
# Hermes' former own-tab preamble opened a second, FOREGROUND tab that stole the visible tab of a watched
# browser and outlived its daemon.

# Env var naming the per-lane tab ledger the prelude below appends to (one targetId per line).
_TABS_FILE_ENV = "HERMES_BH_TABS_FILE"
# Prepended to the model's code while idle-lane reaping is on: records every tab the session opens with
# new_tab() so the reaper can close them after it stops the idle daemon (the daemon itself only closes its
# dedicated tab). Wraps the pre-imported helper in the exec namespace; the original stays reachable.
_TAB_LEDGER_PRELUDE = """\
# hermes: remember tabs this session opens so idle-lane cleanup can close them
def _hermes_track_new_tabs():
    import os as _os
    _path = _os.environ.get("HERMES_BH_TABS_FILE")
    _orig = globals().get("new_tab")
    _cdp = globals().get("cdp")
    if not _path or _orig is None or _cdp is None or getattr(_orig, "_hermes_tracked", False):
        return
    def new_tab(url="about:blank"):
        # new_tab() may REUSE the attached blank tab (possibly the user's): record only targets that did
        # not exist before the call, and nothing at all when the before-set is unknown.
        try:
            _known = {t.get("targetId") for t in _cdp("Target.getTargets").get("targetInfos", [])}
        except Exception:
            _known = None
        _tid = _orig(url)
        if _known is not None and _tid and _tid not in _known:
            try:
                with open(_path, "a", encoding="utf-8") as _f:
                    _f.write("%s\\n" % _tid)
            except Exception:
                pass
        return _tid
    new_tab.__doc__ = _orig.__doc__
    new_tab._hermes_tracked = True
    globals()["new_tab"] = new_tab
_hermes_track_new_tabs()
del _hermes_track_new_tabs
"""

# Idle-lane reaping (``browser.exec_idle_minutes``). A lane is one harness daemon (one BU_NAME) in the
# lanes namespace beside the harness runtime dir (``<dir>-l``, see ``_lanes_runtime_dir``), which only
# stamping callers use. Hermes stamps ``bu-<name>.hermes-lane`` there at every call start; a lane whose
# stamp is older than the idle window is stopped through the harness's own ``--reload`` (its dedicated tab
# closes with it) after the tabs in ``bu-<name>.hermes-tabs`` are closed through the still-live daemon.
# The floor keeps the window above the longest possible call, so a running call is never cut.
_LANE_STAMP_SUFFIX = ".hermes-lane"
_LANE_TABS_SUFFIX = ".hermes-tabs"
_LANE_LOCK_NAME = "hermes-lanes.lock"
_REAP_INTERVAL_S = 300
# A call waits this long for a reap of its runtime dir to finish; > two _STOP_STEP_TIMEOUT_S steps.
_LOCK_WAIT_S = 75.0
_STOP_STEP_TIMEOUT_S = 25
_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# Env keys that decide where the harness keeps a daemon's pid/sock files.
_HARNESS_DIR_ENV_KEYS = ("BH_RUNTIME_DIR", "BH_RUNTIME_DIR_SHARED", "BH_TMP_DIR", "BH_TMP_DIR_SHARED",
                         "BH_HOME", "BROWSER_HARNESS_HOME", "XDG_CONFIG_HOME", "HOME")

_DEFAULT_TIMEOUT_S = 300
_MIN_TIMEOUT_S = 5
_MAX_TIMEOUT_S = 1800
# Shortest idle window: above the longest call, so a lane mid-call is never stopped.
_IDLE_FLOOR_S = _MAX_TIMEOUT_S + 300
_STDERR_CAP_CHARS = 4000

_TASK_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")  # filesystem-safe task ids
# Screenshot paths printed by capture_screenshot(): POSIX or Windows drive-letter absolute.
_IMAGE_PATH_RE = re.compile(r"((?:[A-Za-z]:[\\/]|/)[^\s\"']+?\.(?:png|jpe?g|webp))", re.IGNORECASE)
# http(s) URL literals in exec code checked against browser_navigate's policy
_URL_RE = re.compile(r"https?://[^\s'\"\\)]+", re.IGNORECASE)
_FHS_BIN_DIRS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")


def _quiet(fn: Callable[[], Any], default: Any, log_prefix: str = "") -> Any:
    """``fn()``, or ``default`` on any exception (debug-logged when ``log_prefix`` is set)."""
    try:
        return fn()
    except Exception as e:
        if log_prefix:
            logger.debug("%s: %s", log_prefix, e)
    return default


def _lazy_call(module: str, name: str, default: Any, log_prefix: str) -> Any:
    """Call ``module.name()`` resolved at call time (tests stub the module / patch the
    attribute); on any failure log ``log_prefix`` and return ``default``."""
    return _quiet(lambda: getattr(importlib.import_module(module), name)(), default, log_prefix)


def _camofox_active(context: str = "") -> bool:
    return _lazy_call("tools.browser_camofox", "is_camofox_mode", False, f"Camofox activity check failed{context}")


def _real_profile_consented() -> bool:
    return _lazy_call("tools.browser_tool_cloud", "_use_real_profile", False, "real-profile consent lookup failed")


def _set_cdp_env(env: dict, cdp: str) -> None:
    """Export a CDP endpoint under the BU_CDP_* contract (http(s) → URL, else WS)."""
    env["BU_CDP_URL" if cdp.startswith(("http://", "https://")) else "BU_CDP_WS"] = cdp


def _has_cdp_env(env: dict) -> bool:
    return bool(env.get("BU_CDP_WS") or env.get("BU_CDP_URL"))


def _export_session_cdp(env: dict, get_session_info: Callable[[str], Any], cache_key: str,
                        fail_msg: Callable[[Exception], str], no_cdp_msg: str) -> Optional[str]:
    """Export the CDP endpoint from ``get_session_info(cache_key)``; error string on failure / no CDP."""
    try:
        cdp = str((get_session_info(cache_key) or {}).get("cdp_url") or "")
    except Exception as e:
        return fail_msg(e)
    if not cdp:
        return no_cdp_msg
    _set_cdp_env(env, cdp)
    return None


class _CodeUrlVerdict(NamedTuple):
    """What one pass over a code blob's URL literals decided (see ``_code_url_verdict``)."""

    blocked: Optional[str]
    local_sidecar: bool


def _code_url_verdict(code: str) -> _CodeUrlVerdict:
    """One pass over the code's URL literals: the blocking error, if any, and whether the call must run on
    the hybrid LOCAL sidecar. Each URL's routing predicate is evaluated ONCE and feeds both answers: it
    resolves DNS unmemoised, so a rebinding host evaluated twice could relax the private-address floor and
    still route to the cloud provider."""
    from tools.browser_tool import _hybrid_routes_locally, evaluate_url_safety
    local_sidecar = False
    for url in _URL_RE.findall(code or ""):
        routes_local = _hybrid_routes_locally(url)
        local_sidecar = local_sidecar or routes_local
        err = evaluate_url_safety(url, auto_local=routes_local)
        if err:
            return _CodeUrlVerdict(err.get("error", "Blocked: unsafe URL"), local_sidecar)
    return _CodeUrlVerdict(None, local_sidecar)


def _base_subprocess_env() -> dict:
    from tools.browser_tool import _build_browser_env
    env = _build_browser_env()
    # The harness runs on Hermes's own interpreter, but a bundled Desktop install boots that
    # interpreter with its site dir on PYTHONPATH (no venv to activate), and the harness's daemon
    # re-runs sys.executable. Point PYTHONPATH at the harness's site dir, replacing whatever the
    # agent process inherited, so both the CLI and its daemon import the same packages.
    env.pop("PYTHONHOME", None)
    site_dir = _harness_site_dir()
    if site_dir:
        env["PYTHONPATH"] = site_dir
    else:
        env.pop("PYTHONPATH", None)
    env["PATH"] = _floor_subprocess_path(env.get("PATH", ""))
    env.setdefault("ANONYMIZED_TELEMETRY", "false")
    namespace_harness_dirs(env, _served_profile_tag())  # a served profile's own harness runtime/tmp dirs
    return env


def _floor_subprocess_path(path: str) -> str:
    """Keep system commands reachable from profile workers with a minimal PATH.

    Browser subprocesses may launch shell helpers; version-manager-only paths
    omit /usr/bin. Reuse the shared browser PATH floor on POSIX.
    """
    if os.name == "nt":
        return path
    with contextlib.suppress(Exception):
        from tools.browser_tool_install import _merge_browser_path
        return _merge_browser_path(path or "")
    parts = [p for p in (path or "").split(os.pathsep) if p]
    return os.pathsep.join(parts + [d for d in _FHS_BIN_DIRS if d not in set(parts) and os.path.isdir(d)])


def _read_browser_cfg() -> dict:
    """Return the ``browser:`` config section, or {} on any failure."""
    try:
        from hermes_cli.config import cfg_get, read_raw_config
        cfg = cfg_get(read_raw_config(), "browser", default={})
        return cfg if isinstance(cfg, dict) else {}
    except Exception as e:
        logger.debug("Could not read browser config section: %s", e)
        return {}


def _use_gateway(browser_cfg: dict) -> bool:
    """True when the browser section selects the Nous Tool Gateway — by the current ``hermes tools``
    picker row (``cloud_provider: nous``) or the pre-picker ``use_gateway: true`` flag. Reading only
    the legacy flag missed every picker-configured gateway, and the direct-API branch it fell into
    holds no credentials in managed mode (#108310)."""
    if is_truthy_value(browser_cfg.get("use_gateway"), default=False):
        return True
    try:
        from tools.tool_backend_helpers import NOUS_MANAGED_PROVIDER
    except Exception:  # pragma: no cover — helper ships with the package
        return False
    return str(browser_cfg.get("cloud_provider") or "").strip().lower() == NOUS_MANAGED_PROVIDER


def get_browser_backend() -> str:
    """Configured browser backend key ("" = unset → default). YAML 1.1 parses an
    unquoted ``off`` as False — that must mean BACKEND_DISABLED, not "unset"."""
    raw = _read_browser_cfg().get("backend")
    return (BACKEND_DISABLED if raw is False else "") if isinstance(raw, bool) else str(raw or "").strip().lower()


def set_browser_use_mode(enabled: bool) -> None:
    """``/browser use [off]`` on every surface: persist ``browser.backend`` for the current profile and drop
    cached tool availability. A live agent keeps its tools (prompt cache); the next one built gets the swap."""
    from hermes_cli.config import load_config, save_config
    from tools.registry import invalidate_check_fn_cache
    config = load_config()
    config.setdefault("browser", {})["backend"] = _BACKEND_KEY if enabled else BACKEND_DISABLED
    save_config(config)
    invalidate_check_fn_cache()


def is_legacy_browser_use_cloud_config(browser_cfg: dict) -> bool:
    """True for pre-CLI direct-API Browser Use cloud configs. An explicit backend or
    a non-Browser-Use cloud_provider wins; Camofox is selected via env var, not
    cloud_provider, so a Camofox user with a stray BROWSER_USE_API_KEY keeps it."""
    if not isinstance(browser_cfg, dict) or browser_cfg.get("backend"):
        return False
    provider = str(browser_cfg.get("cloud_provider") or "").strip().lower()
    if provider not in {"browser-use", ""} or _use_gateway(browser_cfg) or _camofox_active(" during migration"):
        return False
    return _browser_use_api_key_set()


def _browser_use_api_key_set() -> bool:
    """BROWSER_USE_API_KEY for the served profile: the only credential Hermes autospawns BU cloud with.
    Profile-scoped so a multiplexed secondary never inherits the default's cloud mode."""
    from agent.secret_scope import get_secret
    return bool(get_secret("BROWSER_USE_API_KEY", ""))


def is_browser_use_cli_mode() -> bool:
    """True when the Browser Use CLI replaces the built-in browser stack. Browser Use mode is the DEFAULT:
    unset ``browser.backend`` ("") enables it whenever browser-harness is importable (a core dependency);
    ``browser.backend: off`` keeps the built-in browser_* tools. Camofox always falls back to the built-in
    tools (Firefox, custom HTTP API, no CDP surface for the harness)."""
    if _camofox_active():
        return False
    backend = get_browser_backend()
    return backend == _BACKEND_KEY if backend else (is_legacy_browser_use_cloud_config(_read_browser_cfg()) or _find_cli() is not None)


def default_downgrade_notice() -> Optional[str]:
    """One-line notice when ``browser.backend`` is unset but the CLI is not runnable, so
    the session fell back to the built-in tools. Rate-limited to once per 24h via a stamp file."""
    try:
        if get_browser_backend() or _camofox_active() or _find_cli() is not None:
            return None  # explicit choice / Camofox / CLI present — nothing downgraded
        stamp = Path(get_hermes_home()) / "cache" / ".browser_use_default_notice"
        now = time.time()
        with contextlib.suppress(OSError):
            if 0 <= now - stamp.stat().st_mtime < 24 * 3600:
                return None
        with contextlib.suppress(OSError):
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.touch()
            os.utime(stamp, (now, now))
        return ("browser-harness is missing from Hermes's Python environment — using the built-in browser tools. "
                "Run `hermes update` to re-sync it, or set `browser.backend: off` in config.yaml to silence this.")
    except Exception as e:  # pragma: no cover — a notice must never break startup
        logger.debug("browser-use downgrade notice failed: %s", e)
        return None


def _harness_site_dir() -> Optional[str]:
    """The site dir Hermes's interpreter imports ``browser_harness`` from, or None."""
    spec = importlib.util.find_spec("browser_harness")
    if spec is None or not spec.origin:
        return None
    return str(Path(spec.origin).resolve().parent.parent)


def _find_cli() -> Optional[list[str]]:
    """The Browser Use CLI's engine (browser-harness) is a core dependency of Hermes's own venv,
    so every install, the Desktop bundle included, runs it on the current interpreter."""
    if _harness_site_dir() is None:
        return None
    return [sys.executable, "-m", "browser_harness.run"]


def _workspace_dir(task_id: Optional[str]) -> Optional[str]:
    """Stable per-task scratch dir that persists across browser_exec calls"""
    if os.environ.get("BH_AGENT_WORKSPACE"):
        return os.environ["BH_AGENT_WORKSPACE"]
    try:
        safe = _TASK_ID_SAFE_RE.sub("_", str(task_id or "default"))[:80] or "default"
        path = Path(get_hermes_home()) / "cache" / "browser-use" / "workspace" / safe
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
    except Exception as e:
        logger.debug("browser_exec workspace unavailable: %s", e)
        return None


def _find_screenshot(stdout: str, since: float) -> Optional[str]:
    """Last screenshot path printed during this exec that exists and was written after
    the exec started, or None."""
    for path in reversed(_IMAGE_PATH_RE.findall(stdout or "")):
        try:
            if os.path.isfile(path) and os.path.getmtime(path) >= since - 1:
                return path
        except OSError:
            continue
    return None


def _native_screenshot_result(result: dict[str, Any], path: str) -> Optional[dict[str, Any]]:
    """Build a multimodal tool result attaching path for vision models"""
    try:
        from tools.vision_tools import (_EMBED_MAX_DIMENSION,
                                        _resize_image_for_vision, _should_use_native_vision_fast_path)
        from tools.vision_tools_history_budget import resolve_embed_target_bytes
        if not _should_use_native_vision_fast_path():
            return None
        # History-reuse cap: this data URL bakes into the tool result and is re-sent every later turn —
        # same policy as the vision_analyze / browser_vision native embeds.
        data_url = _resize_image_for_vision(Path(path), mime_type="image/png",
                                            max_base64_bytes=resolve_embed_target_bytes(),
                                            max_dimension=_EMBED_MAX_DIMENSION, force_jpeg=True)
        text = json.dumps(result, ensure_ascii=False)
        attached = text + "\n\nThe screenshot from this call is attached — inspect it with your native vision."
        return {"_multimodal": True, "text_summary": text, "meta": {"screenshot_path": path, "native_vision": True},
                "content": [{"type": "text", "text": attached}, {"type": "image_url", "image_url": {"url": data_url}}]}
    except Exception as e:
        logger.debug("Native screenshot attach failed (falling back to text): %s", e)
        return None


def _served_profile_tag() -> str:
    """``""`` outside a served-profile scope (every legacy key stays byte-identical); under a
    multiplexed turn, the routed profile's home key — one profile's browser must never be handed
    to another that happens to use the same session name or task id (#110032)."""
    from hermes_constants import get_hermes_home_override, hermes_home_key
    return "" if get_hermes_home_override() is None else hermes_home_key()


def _isolate_served_profile_daemons(env: dict) -> None:
    """Give a served profile its own harness daemon namespace.

    browser-harness keys its daemon (pid/socket/log) by ``BU_NAME`` under a per-user runtime dir. Under
    multiplex every profile runs as the same OS user, so a served profile's unnamed ``browser_exec``
    reached the launch profile's live ``bu-default`` daemon — already attached to *that* profile's browser
    (its ``cdp_url``, proxy, real-profile Chrome) — and ignored its own backend entirely. The daemon is
    reused before the freshly resolved ``BU_CDP_*`` is ever read, so the per-profile session cache key
    (#110032) alone cannot prevent it. The same collision hits a standalone ``hermes -p <name>`` (subprocess
    HOME stays the real one), so any named profile gets its own namespace; the default profile keeps the
    legacy per-user runtime dir.
    """
    tag = _served_profile_tag()
    if not tag:
        from hermes_constants import get_hermes_home, hermes_home_key, profile_name_for_home
        home = get_hermes_home()
        tag = "" if profile_name_for_home(home) in (None, "default") else hermes_home_key(home)
    if not tag or env.get("BH_RUNTIME_DIR"):
        return
    import hashlib
    import tempfile
    digest = hashlib.sha256(tag.encode("utf-8")).hexdigest()[:12]
    # Short root: the daemon's AF_UNIX socket path is capped at ~108 bytes (``TMPDIR`` is socket-safe here).
    root = os.path.join(env.get("TMPDIR") or tempfile.gettempdir(), f"bh-{digest}")
    env["BH_RUNTIME_DIR"] = root
    env["BH_RUNTIME_DIR_SHARED"] = "1"  # still many BU_NAMEs per profile: keep the name in each filename
    env["BH_TMP_DIR"] = root
    env["BH_TMP_DIR_SHARED"] = "1"


def _backend_cache_key(task_id: Optional[str], session_name: str = "", sidecar: bool = False) -> str:
    """Session-cache key for a backend browser: named sessions get their own; served profiles get their own.
    ``sidecar`` appends the hybrid ``::local`` suffix, which makes ``_get_session_info`` build local Chromium
    even when a cloud provider is configured."""
    key = f"bu-named-{session_name}" if session_name else (task_id or "browser-exec-default")
    tag = _served_profile_tag()
    key = f"{key}@{tag}" if tag else key
    return f"{key}::local" if sidecar else key


def _sidecar_daemon_name(name: str) -> str:
    """BU_NAME of the harness daemon serving the hybrid local sidecar. The daemon reads BU_CDP_* once at
    spawn and outlives the call, so sharing the cloud route's daemon would relay a private URL to the
    cloud browser it is already attached to. User session names cannot start with ``_``."""
    if not name:
        return "_local_default"
    import hashlib
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return f"_local_{name[:40]}_{digest}"


def _export_real_profile_note(env: dict, key: str) -> None:
    """The session layer decides real profile vs fresh browser once per key; carry its note (if it fell
    back) so the result reports the browser this call actually drives."""
    from tools.browser_tool_session import real_profile_note
    note = real_profile_note(key)
    if note:
        env.setdefault(_REAL_PROFILE_FALLBACK_SENTINEL, note)


def _resolve_lightpanda_cdp(env: dict, task_id: Optional[str], session_name: str = "",
                            sidecar: bool = False) -> Optional[str]:
    """Point the harness at a Hermes-spawned ``lightpanda serve`` (``browser.engine: lightpanda`` and
    nothing of higher precedence claimed the session). Each cache key gets its own process via the
    legacy ``_get_session_info()`` (cache, reaper, atexit): private browser, own-tab preamble skipped."""
    try:
        from tools.browser_tool_session import _get_session_info
        from tools.browser_tool_lightpanda_fallback import _using_lightpanda_engine
        if not _using_lightpanda_engine():
            return None
    except Exception as e:  # stubbed browser_tool in tests / engine lookup failure
        logger.debug("browser engine lookup failed: %s", e)
        return None
    err = _export_session_cdp(
        env, _get_session_info, _backend_cache_key(task_id, session_name, sidecar),
        lambda e: (f"Lightpanda could not be started: {e} Set browser.engine to auto "
                   "to use local Chrome, or switch backends via `hermes tools` → Browser Automation."),
        "Lightpanda session returned no CDP endpoint. Set browser.engine to auto to use local Chrome.",
    )
    if err is None:
        env[_PRIVATE_BROWSER_SENTINEL] = "1"
        env[_BOT_DESKTOP_BROWSER_SENTINEL] = "1"
        _export_real_profile_note(env, _backend_cache_key(task_id, session_name, sidecar))
    return err


def _reach_sandbox_cdp(cdp: str) -> str:
    """A CDP endpoint agent-browser reported from INSIDE the terminal backend's sandbox is that sandbox's
    loopback: unreachable from this host (Docker bridge / ssh remote). The harness, the vault supervisor
    and ``browser_exec`` all connect from here, so forward the port over the sandbox's exec stream and hand
    them the local end. Chromium's DevTools accepts any loopback ``Host`` header, port included."""
    try:
        from tools.browser_tool_session import _browser_in_sandbox
        if not _browser_in_sandbox():
            return cdp
        from urllib.parse import urlsplit, urlunsplit
        from tools.bot_desktop import runtime as _bd_runtime, sandbox_host
        from tools.environments import streams
        parts = urlsplit(cdp)
        if parts.hostname not in ("127.0.0.1", "localhost") or not parts.port:
            return cdp
        sandbox = _bd_runtime._sandbox_env(create=True)
        if sandbox is None:
            return cdp
        local = streams.forward_port(sandbox, parts.port, user=sandbox_host._user_for(sandbox))
        return urlunsplit(parts._replace(netloc=f"127.0.0.1:{local}"))
    except Exception as e:  # a failed forward degrades to the unreachable endpoint's own error
        logger.debug("sandbox CDP forward unavailable: %s", e)
        return cdp


def _resolve_managed_chromium_cdp(env: dict, task_id: Optional[str], session_name: str = "",
                                  sidecar: bool = False) -> Optional[str]:
    """Point the harness at Hermes' packaged Chromium, launched through agent-browser for this cache key —
    the same browser the built-in tools drive. Left alone, the harness discovers the user's INSTALLED
    Chrome on its default profile, which needs the chrome://inspect toggle + an Allow popup per run and
    is blocked outright on Chrome >=136; on a headless box it just reports ``chrome-not-running``.
    ``get cdp-url`` runs through ``_run_browser_command`` (legacy cache, inactivity reaper, atexit, Chromium
    preflight/auto-install) on EVERY call: it launches the browser cold, follows a relaunch, and refreshes
    the agent-browser daemon's idle timer, which never sees the harness's direct CDP traffic."""
    try:
        from tools.browser_tool_session import _run_browser_command
        from tools.browser_tool import _get_open_command_timeout
    except Exception as e:  # pragma: no cover — stubbed browser_tool in tests
        logger.debug("managed chromium resolution unavailable: %s", e)
        return None
    res = _run_browser_command(_backend_cache_key(task_id, session_name, sidecar), "get", ["cdp-url"],
                               timeout=_get_open_command_timeout(first_open=True))
    cdp = str(((res or {}).get("data") or {}).get("cdpUrl") or "") if (res or {}).get("success") else ""
    if not cdp:
        return (f"The local browser could not be started: {(res or {}).get('error') or 'agent-browser returned no CDP endpoint'} "
                "Run `hermes tools` → Browser Automation to (re)install Chromium, or switch backends.")
    cdp = _reach_sandbox_cdp(cdp)
    _set_cdp_env(env, cdp)
    env[_PRIVATE_BROWSER_SENTINEL] = "1"  # one Chromium per cache key: nothing to share a tab with
    env[_BOT_DESKTOP_BROWSER_SENTINEL] = "1"
    _export_real_profile_note(env, _backend_cache_key(task_id, session_name, sidecar))
    return None


def _resolve_local_engine_cdp(env: dict, task_id: Optional[str], session_name: str = "",
                              sidecar: bool = False) -> Optional[str]:
    """Local engine (no provider / override, or the hybrid sidecar): ``browser.engine: lightpanda`` or the
    packaged Chromium."""
    err = _resolve_lightpanda_cdp(env, task_id, session_name, sidecar)
    if err or _has_cdp_env(env):
        return err
    return _resolve_managed_chromium_cdp(env, task_id, session_name, sidecar)


def _resolve_backend_cdp(env: dict, task_id: Optional[str], session_name: str = "",
                         force_local_sidecar: bool = False) -> Optional[str]:
    """Point the harness at the configured backend's CDP endpoint; error string on failure.

    Precedence: (1) ``BU_CDP_WS``/``BU_CDP_URL`` already in env (operator override); (2) ``BROWSER_CDP_URL``
    env / ``browser.cdp_url`` (``/browser connect``); (3) a cloud provider via the legacy ``_get_session_info()``
    so browser_exec shares the SAME session machinery (per-task cache, expiry, reaper, atexit);
    (4) the local engine — ``browser.engine: lightpanda`` or Hermes' packaged Chromium via agent-browser
    (never the harness's own discovery of the user's installed Chrome); (5) BU direct-API configs WITH
    ``BROWSER_USE_API_KEY`` → None (checked before (3)): the CLI reaches BU cloud natively (BU_AUTOSPAWN); a
    keyless ``browser-use`` provider (managed-gateway autodetect, or a selection missing its key) takes (3).
    ``session_name`` (BU_NAME) keys the session cache so each name gets its OWN browser — what makes named
    sessions concurrent-safe.

    ``force_local_sidecar`` skips step (3): the call runs on the local engine under the ``::local`` cache
    key (hybrid routing; see ``_hybrid_routes_locally``). It sits BELOW the two operator overrides, and the
    sidecar verdict never fires while a CDP override is set."""
    if _has_cdp_env(env):
        env[_SHARED_BROWSER_SENTINEL] = "1"  # operator BU_CDP_* or the real-profile browser
        return None
    try:
        from tools.browser_tool_cloud import _get_cloud_provider
        from tools.browser_tool_session import _get_session_info
        from tools.browser_tool_cdp import _get_cdp_override
    except Exception as e:  # pragma: no cover — stubbed browser_tool in tests
        logger.debug("browser_tool backend resolution unavailable: %s", e)
        return None
    override = _quiet(_get_cdp_override, "")
    if override:
        _set_cdp_env(env, override)
        env[_SHARED_BROWSER_SENTINEL] = "1"
        return None
    if force_local_sidecar:
        return _resolve_local_engine_cdp(env, task_id, session_name, sidecar=True)
    provider = _quiet(_get_cloud_provider, None, "Cloud provider lookup failed")
    if provider is None:
        return _resolve_local_engine_cdp(env, task_id, session_name)

    # Browser Use direct-API configs: the CLI talks to BU cloud natively (BU_AUTOSPAWN) — the
    # legacy provider would create a second, redundant session. Nous-gateway configs (cloud_provider: nous
    # from the picker, or the pre-picker use_gateway: true) DO resolve through the provider: the gateway
    # provisions the browser server-side and returns its CDP URL. Without BROWSER_USE_API_KEY the CLI cannot
    # autospawn, and the provider is either the managed gateway (autodetect on a never-configured install) or
    # a selection missing its key; both go through the session path (gateway CDP, or the session layer's
    # local-Chromium fallback) instead of leaving the harness to hunt for an installed Chrome.
    provider_key = str(getattr(provider, "name", "") or "").strip().lower()
    if provider_key == _BACKEND_KEY and not _use_gateway(_read_browser_cfg()) and _browser_use_api_key_set():
        env[_PRIVATE_BROWSER_SENTINEL] = "1"  # named BU cloud browsers are exclusive to their daemon
        return None

    provider_name = type(provider).__name__
    session: dict = {}

    def _session_info(key: str) -> dict:
        session.update(_get_session_info(key) or {})
        return session

    err = _export_session_cdp(
        env, _session_info, _backend_cache_key(task_id, session_name),
        lambda e: (f"Cloud browser provider {provider_name} failed to provide a session: {e}. "
                   "Fix the provider configuration or switch backends via `hermes tools` → Browser Automation."),
        f"Cloud browser provider {provider_name} returned no CDP endpoint, so Browser Use mode "
        "cannot drive it. Switch to the built-in browser tools for this provider.",
    )
    if err and session.get("fallback_from_cloud"):
        # The provider failed (rate limit, quota, outage) and the session layer fell back to local
        # Chromium, as it does for the built-in tools; that session has no CDP URL until it launches.
        logger.warning("browser_exec: %s failed (%s); using local Chromium for this session",
                       provider_name, session.get("fallback_reason"))
        return _resolve_managed_chromium_cdp(env, task_id, session_name)
    # A provider browser keyed bu-named-<name> is exclusive to this session — the
    # own-tab preamble would just leak a blank tab into it.
    if err is None and session_name:
        env[_PRIVATE_BROWSER_SENTINEL] = "1"
    return err


def _resolve_real_profile_cdp(env: dict, force_local: bool) -> Optional[str]:
    """Point the harness at the user's real-profile copy-browser (a SNAPSHOT of their default Chromium
    profile, hermes_cli.browser_connect) when consented. Two ways in: the effective backend is already local
    (no provider, CDP override, or legacy BU cloud config) → silent upgrade; or ``force_local`` (consent-gated
    ``local`` arg) → the user's browser even under a cloud backend. Operator overrides (BU_CDP_* env,
    /browser connect, ``browser.cdp_url``) own the session either way. A launch error is returned;
    ``_route_backend`` then falls back to the normal backend and reports it, never silently."""
    if not _real_profile_consented():
        try:
            from tools.browser_tool_real_profile import _revoke_real_profile_for_current_home
            _revoke_real_profile_for_current_home()
        except Exception as e:
            logger.debug("real-profile revocation cleanup failed: %s", e)
        return None
    if _has_cdp_env(env):
        return None
    try:
        from tools.browser_tool_cdp import _get_cdp_override_raw
        from tools.browser_tool_cloud import _get_cloud_provider
        from tools.browser_tool_real_profile import _real_profile_cdp
    except Exception as e:  # pragma: no cover — stubbed browser_tool in tests
        logger.debug("real-profile backend resolution unavailable: %s", e)
        return None
    if _quiet(_get_cdp_override_raw, ""):
        return None
    # Only auto-upgrade genuinely-local attaches; any cloud path (provider, provider lookup failure, or
    # legacy BU cloud config) stays on its backend unless the model passes local=true.
    if not force_local and (_quiet(_get_cloud_provider, object()) is not None
                            or is_legacy_browser_use_cloud_config(_read_browser_cfg())):
        return None
    cdp, err = _real_profile_cdp()
    if cdp and not err:
        _set_cdp_env(env, cdp)
        env[_BOT_DESKTOP_BROWSER_SENTINEL] = "1"
    return err or None


def _attach_vault_supervisor(env: dict, task_id: Optional[str]) -> None:
    """Attach the per-task CDP supervisor to the browser this exec drives so ``browser_vault`` fill has
    a secret-capable WebSocket (never argv) into the SAME browser. Only CDP-routed backends expose an
    endpoint; BU direct-cloud (BU_AUTOSPAWN) does not, and the vault tools report ``supervisor_required``."""
    cdp = env.get("BU_CDP_WS") or env.get("BU_CDP_URL")
    if not cdp:
        return
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY
        from tools.browser_tool_cdp import _get_dialog_policy_config, _resolve_cdp_override
        policy, timeout_s = _get_dialog_policy_config()
        SUPERVISOR_REGISTRY.get_or_start(task_id=task_id or "default", cdp_url=_resolve_cdp_override(cdp),
                                         dialog_policy=policy, dialog_timeout_s=timeout_s)
    except Exception as exc:
        logger.debug("browser_exec: CDP supervisor attach failed (non-fatal): %s", exc)


def _route_backend(env: dict, session: str, task_id: Optional[str], local: bool,
                   force_local_sidecar: bool = False, real_profile: bool = True) -> Optional[str]:
    """Resolve where the harness connects; returns an error string or None. Real-profile consent runs
    BEFORE provider resolution so a hit short-circuits the cloud path via the BU_CDP_* env contract. Named
    sessions compose with the backend: BU_NAME namespaces the harness daemon (IPC socket, log, pid) and on
    provider backends additionally keys its own cloud browser.

    ``force_local_sidecar`` (a call carrying a private URL) declines the cloud provider for the hybrid local
    sidecar; unlike ``local`` (the consent-gated real-profile browser) it never touches the user's profile."""
    # ``real_profile=False``: this route already fell back, so it stays on its fresh browser instead
    # of swapping browsers mid-task once the profile frees up.
    rp_err = _resolve_real_profile_cdp(env, force_local=local) if real_profile else None
    # local=True is only served by the real-profile route; consent off must not pretend.
    if local and not _has_cdp_env(env) and not _real_profile_consented():
        return ("local=true was requested but browser.use_real_profile is off. Enable it in config.yaml "
                "(browser.use_real_profile: true) or the desktop Settings → Browser section, then retry.")
    # local=True promised the user's own machine: a profile that can't load runs on a fresh LOCAL browser
    # (the sidecar, which never loads the profile), never the cloud one. Without ``local`` the session
    # layer makes the one real-profile-or-fresh decision and reports it (_resolve_managed_chromium_cdp).
    if local and not _has_cdp_env(env) and (rp_err or not real_profile):
        if rp_err:
            from tools.browser_tool_real_profile import real_profile_fallback_note
            env[_REAL_PROFILE_FALLBACK_SENTINEL] = real_profile_fallback_note(rp_err)
        env[_LOCAL_FALLBACK_SENTINEL] = "1"
        force_local_sidecar = True
    return _resolve_backend_cdp(env, task_id, session_name=session,
                                force_local_sidecar=force_local_sidecar)


def _group_popen_kwargs() -> dict:
    """Popen kwargs starting the CLI in its own process group (a new session on POSIX) so a
    timeout can take down every process that inherited the capture pipes, not just the CLI
    child. Windows also hides the console the .cmd shim would flash (as browser_tool does)."""
    def _flags() -> dict:
        from hermes_cli._subprocess_compat import windows_hide_flags
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return {"creationflags": windows_hide_flags() | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                "startupinfo": si}
    return _quiet(_flags, {}, "Windows hide-flags unavailable") if os.name == "nt" else {"start_new_session": True}


def _clamp_timeout(timeout_s: Any) -> int:
    try:
        return max(_MIN_TIMEOUT_S, min(int(timeout_s), _MAX_TIMEOUT_S))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_S


# After a whole-group SIGKILL, every pipe holder is dead, so the drain below is normally
# instant; the deadline only guards against a process outside the group still holding a pipe.
_POST_KILL_DRAIN_S = 10.0


def _kill_cli_process_group(proc) -> None:
    """SIGKILL the CLI's whole process group (POSIX; ``start_new_session`` made pgid == pid) or,
    on Windows, its process tree via ``taskkill /T /F`` — the only group-wide kill it offers."""
    if os.name == "nt":
        from hermes_cli._subprocess_compat import windows_hide_flags
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                           check=False, creationflags=windows_hide_flags())
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)  # windows-footgun: ok — POSIX only, the nt branch returned above


def _run_cli_killing_process_group(cmd, code, env, timeout):
    """Run the CLI in its own process group and kill the whole group on timeout.

    ``subprocess.run`` only kills the direct child on ``TimeoutExpired``; a grandchild that
    inherited the stdout/stderr pipes (browser_harness daemon / Chrome helper) is orphaned
    still holding them, and on Windows ``run()``'s unbounded post-kill ``communicate()`` then
    blocks on pipe EOF forever — so the tool call, plus its activity heartbeat, wedges (#106244).
    """
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", env=env, **_group_popen_kwargs(),
    )
    try:
        stdout, stderr = proc.communicate(input=code, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_cli_process_group(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=_POST_KILL_DRAIN_S)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


# Harness daemons this process drove: each latches BU_CDP_* at start and outlives every call, so a backend
# swap (/browser connect|disconnect) leaves later calls in the old browser until these are stopped.
_driven_daemons: dict[tuple[str, ...], str] = {}  # daemon identity -> home
_driven_daemons_lock = threading.Lock()
_desktop_daemon_epochs: dict[tuple[str, str], int] = {}
_live_generation = functools.partial(_lazy_call, "tools.browser_tool_cdp", "live_cdp_generation", 0,
                                      "live CDP generation lookup failed")  # this profile's publish count
# Route cache key -> whether its last browser_exec ran on the hybrid local sidecar.
_exec_route_is_sidecar: dict[str, bool] = {}
# Route cache key -> the note from its real-profile fallback; that route stays on the fallback browser.
_real_profile_fallback: dict[str, str] = {}
_real_profile_note_delivered: set = set()


def stop_harness_daemons(home_key: Optional[str] = None) -> None:
    """Stop every harness daemon this process drove (only ``home_key``'s when given), through the
    harness's own identity-checked ``--reload``; the next browser_exec respawns one on the endpoint
    it resolves then."""
    with _driven_daemons_lock:
        daemons = sorted(key for key, home in _driven_daemons.items() if home_key in (None, home))
        for key in daemons:
            _driven_daemons.pop(key, None)
    _reload_daemons(daemons)


def _reload_daemons(daemons) -> None:  # each ``daemon_identity`` daemon, with its own dirs
    cmd = _find_cli() if daemons else None
    if not cmd:
        return
    env = _base_subprocess_env()
    for daemon in daemons:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            _run_cli_killing_process_group([*cmd, "--reload"], "", daemon_env(env, daemon), 15)


def _exec_task_tabs_enabled(browser_cfg: dict) -> bool:
    """``browser.exec_task_tabs``: on a SHARED browser, an unnamed call runs in this task's own lane."""
    return is_truthy_value(browser_cfg.get("exec_task_tabs", False))


def _exec_idle_seconds(browser_cfg: dict) -> int:
    """``browser.exec_idle_minutes`` as seconds; 0 = never reap. Clamped up to ``_IDLE_FLOOR_S``."""
    try:
        minutes = float(browser_cfg.get("exec_idle_minutes", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(int(minutes * 60), _IDLE_FLOOR_S) if minutes > 0 else 0


def _task_session_name(task_id: Optional[str]) -> str:
    """Harness daemon name for a task's unnamed calls: readable, valid BU_NAME, distinct per task id."""
    raw = str(task_id or "")
    if not raw:
        return ""
    import hashlib
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-_")[:26]  # <= 40 chars: socket path budget
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"task-{safe}-{digest}" if safe else f"task-{digest}"


def _harness_runtime_dir(env: dict) -> Optional[Path]:
    """Where the harness keeps ``bu-<name>.pid`` for this env (mirrors ``browser_harness._ipc``), or None
    for a caller-isolated single-daemon dir (bare ``bu.*`` stems carry no name to reap by)."""
    raw = env.get("BH_RUNTIME_DIR") or env.get("BH_TMP_DIR")
    if raw:
        if env.get("BH_RUNTIME_DIR_SHARED") != "1":
            return None
        return Path(raw).expanduser()
    home = env.get("BH_HOME") or env.get("BROWSER_HARNESS_HOME")
    if home:
        return Path(home).expanduser() / "runtime"
    if env.get("XDG_CONFIG_HOME"):
        return Path(env["XDG_CONFIG_HOME"]).expanduser() / "browser-harness" / "runtime"
    base = Path(env["HOME"]) if env.get("HOME") else Path.home()
    return base / ".config" / "browser-harness" / "runtime"


class _LaneDir(NamedTuple):
    env: dict[str, str]  # the harness-dir env keys a CLI subprocess needs to reach these daemons
    idle_s: int


# Harness runtime dirs this process has driven with reaping on -> how to reach and judge their lanes.
_lane_dirs: dict[str, _LaneDir] = {}
_lane_inflight: dict[tuple, int] = {}  # (runtime dir, BU_NAME) -> calls running in this process
_lane_state_lock = threading.Lock()
_lane_reaper_thread: Optional[threading.Thread] = None


_LOCK_HELD, _LOCK_TIMEOUT, _LOCK_UNAVAILABLE = "held", "timeout", "unavailable"


@contextlib.contextmanager
def _lane_dir_lock(runtime_dir: Path, wait_s: Optional[float] = None):
    """Cross-process lock on one runtime dir, serializing call stamps against reaps. Yields
    ``_LOCK_HELD``, ``_LOCK_TIMEOUT`` (another holder kept it past ``wait_s``) or ``_LOCK_UNAVAILABLE``
    (no ``fcntl`` / lock file unopenable — then no reaper can hold it either)."""
    try:
        import fcntl
    except ImportError:  # Windows: no reaping (see _touch_lane), nothing to serialize
        yield _LOCK_UNAVAILABLE
        return
    try:
        fd = os.open(str(runtime_dir / _LANE_LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        yield _LOCK_UNAVAILABLE
        return
    held = False
    try:
        deadline = time.monotonic() + (_LOCK_WAIT_S if wait_s is None else wait_s)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield _LOCK_HELD if held else _LOCK_TIMEOUT
    finally:
        if held:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _lane_files(runtime_dir: Path, name: str) -> tuple[Path, Path]:
    return runtime_dir / f"bu-{name}{_LANE_STAMP_SUFFIX}", runtime_dir / f"bu-{name}{_LANE_TABS_SUFFIX}"


class _LaneTouch(NamedTuple):
    tabs: Optional[Path]  # tab ledger to export as HERMES_BH_TABS_FILE; None = lane not tracked
    busy: bool = False  # a reap of this runtime dir held the lock past the wait: do not dispatch


def _lanes_runtime_dir(runtime_dir: Path) -> Path:
    """The idle-lane namespace beside a harness runtime dir. Only callers that stamp every call under the
    dir lock ever start daemons here, so an older Hermes (or a caller with reaping off) that drives the
    same BU_NAME gets its own daemon in the original dir and can never be reaped. Kept short: the
    daemon's AF_UNIX socket path lives in it."""
    return runtime_dir.with_name(runtime_dir.name + "-l")


def _touch_lane(env: dict, name: str, idle_s: int) -> _LaneTouch:
    """Join the idle-lane protocol for this call: stamp lane ``name`` as used now (under the dir lock, so
    no reap can be mid-flight), point ``env`` at the lanes namespace, and register it for reaping. A stamp
    is only written while the lock is held; if a reap keeps it past the wait the call must not start,
    because that reap may be about to stop this very daemon. On any other failure ``env`` is left on the
    original namespace and the call runs untracked."""
    if os.name == "nt" or idle_s <= 0 or name == "default":
        return _LaneTouch(None)
    base_dir = _harness_runtime_dir(env)
    if base_dir is None:
        return _LaneTouch(None)
    runtime_dir = _lanes_runtime_dir(base_dir)
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        return _LaneTouch(None)
    stamp, tabs = _lane_files(runtime_dir, name)
    with _lane_dir_lock(runtime_dir) as lock:
        if lock == _LOCK_TIMEOUT:
            return _LaneTouch(None, busy=True)
        if lock != _LOCK_HELD:
            return _LaneTouch(None)  # nobody can lock, so nobody can reap: run untracked
        try:
            stamp.touch()
        except OSError:
            return _LaneTouch(None)
    lanes_env = {"BH_RUNTIME_DIR": str(runtime_dir), "BH_RUNTIME_DIR_SHARED": "1",
                 "BH_TMP_DIR": str(runtime_dir), "BH_TMP_DIR_SHARED": "1"}
    env.update(lanes_env)
    with _lane_state_lock:
        _lane_dirs[str(runtime_dir)] = _LaneDir({k: env[k] for k in _HARNESS_DIR_ENV_KEYS if k in env}, idle_s)
    _ensure_lane_reaper()
    return _LaneTouch(tabs)


def _ledger_tab_ids(tabs: Path) -> list[str]:
    try:
        lines = [line.strip() for line in tabs.read_text(encoding="utf-8-sig").splitlines()]
    except OSError:
        return []
    return list(dict.fromkeys(t for t in lines if _TARGET_ID_RE.match(t)))


def _stop_lane(dir_env: dict[str, str], name: str, tab_ids: list[str]) -> None:
    """Close the lane's recorded tabs through its live daemon, then stop the daemon (closing its dedicated
    tab). ``BH_REQUIRE_EXISTING_DAEMON`` keeps the close call from spawning a fresh daemon. Each step is
    bounded by ``_STOP_STEP_TIMEOUT_S``, so a reap holds the dir lock well under ``_LOCK_WAIT_S``."""
    cmd = _find_cli()
    if not cmd:
        return
    env = {**_base_subprocess_env(), **dir_env, "BU_NAME": name}
    for key in ("BU_CDP_URL", "BU_CDP_WS", "BU_AUTOSPAWN", _TABS_FILE_ENV):
        env.pop(key, None)
    if tab_ids:
        close_code = (f"for _tid in {tab_ids!r}:\n    try:\n        cdp('Target.closeTarget', targetId=_tid)\n"
                      "    except Exception:\n        pass\n")
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            _run_cli_killing_process_group(cmd, close_code, {**env, "BH_REQUIRE_EXISTING_DAEMON": "1"},
                                           _STOP_STEP_TIMEOUT_S)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        _run_cli_killing_process_group([*cmd, "--reload"], "", env, _STOP_STEP_TIMEOUT_S)


def _reap_idle_lanes_once(now: Optional[float] = None) -> list[str]:
    """One pass over every registered runtime dir: stop lanes idle past their window. Only lanes Hermes
    stamped are candidates; a daemon with no stamp (an older Hermes, or another caller that never stamps)
    is never touched, since its activity is invisible here. The shared ``default`` lane is never reaped.
    Returns the reaped lane names."""
    with _lane_state_lock:
        dirs = dict(_lane_dirs)
    reaped: list[str] = []
    for dir_key, lane_dir in dirs.items():
        runtime_dir = Path(dir_key)
        try:
            names = sorted(p.name[3:-len(_LANE_STAMP_SUFFIX)] for p in runtime_dir.glob(f"bu-*{_LANE_STAMP_SUFFIX}"))
        except OSError:
            continue
        for name in names:
            if name == "default" or (not _SESSION_RE.match(name) and not name.startswith("_local_")):
                continue
            with _lane_state_lock:
                if _lane_inflight.get((dir_key, name)):
                    continue
            stamp, tabs = _lane_files(runtime_dir, name)
            with _lane_dir_lock(runtime_dir, wait_s=1.0) as lock:
                if lock != _LOCK_HELD:
                    continue
                current = time.time() if now is None else now
                try:
                    age = current - stamp.stat().st_mtime
                except OSError:
                    continue
                if age < lane_dir.idle_s:
                    continue
                if (runtime_dir / f"bu-{name}.pid").exists():
                    _stop_lane(lane_dir.env, name, _ledger_tab_ids(tabs))
                    reaped.append(name)
                    logger.info("browser_exec: stopped idle browser lane %s (idle %d min)", name, int(age // 60))
                for path in (stamp, tabs):
                    with contextlib.suppress(OSError):
                        path.unlink()
    return reaped


def _lane_reaper_loop() -> None:
    while True:
        time.sleep(_REAP_INTERVAL_S)
        try:
            _reap_idle_lanes_once()
        except (OSError, UnicodeError, subprocess.SubprocessError) as e:
            logger.debug("browser_exec: idle lane reap failed: %s", e)


def _ensure_lane_reaper() -> None:
    global _lane_reaper_thread
    with _lane_state_lock:
        if _lane_reaper_thread is not None and _lane_reaper_thread.is_alive():
            return
        _lane_reaper_thread = threading.Thread(target=_lane_reaper_loop, name="browser-lane-reaper", daemon=True)
        _lane_reaper_thread.start()


def _reconnect_desktop_harness(cmd, env, task_id, epoch):
    if epoch is None:
        return
    from tools.desktop_admission import DesktopInputDenied, assert_agent_admitted
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    key = (_served_profile_tag(), env.get("BU_NAME", "default"))
    with _driven_daemons_lock:
        previous = _desktop_daemon_epochs.get(key)
        if previous is not None and previous != epoch:
            try:
                stopped = _run_cli_killing_process_group([*cmd, "--reload"], "", env, 15)
            except (OSError, subprocess.SubprocessError):
                raise DesktopInputDenied("Desktop reconnect unavailable") from None
            if stopped.returncode != 0:
                raise DesktopInputDenied("Desktop reconnect unavailable")
            SUPERVISOR_REGISTRY.stop(task_id or "default")
            if assert_agent_admitted(env.get("BU_CDP_WS") or env.get("BU_CDP_URL") or "") != epoch:
                raise DesktopInputDenied("Desktop lease changed during reconnect")
        _desktop_daemon_epochs[key] = epoch


def _route_exec(env: dict, session: str, task_id: Optional[str], local: bool, local_sidecar: bool,
                route_key: str) -> tuple[Optional[str], bool]:
    """``_route_backend`` for one browser_exec call; returns ``(error, runs on the local sidecar)``.
    A real-profile fallback is remembered per route: the route keeps its fresh browser and the note
    waits until a result carries it (``_take_real_profile_note``)."""
    route_err = _route_backend(env, session, task_id, local, force_local_sidecar=local_sidecar,
                               real_profile=route_key not in _real_profile_fallback)
    note = env.pop(_REAL_PROFILE_FALLBACK_SENTINEL, None)
    if note:
        _real_profile_fallback.setdefault(route_key, note)
    return route_err, local_sidecar or bool(env.pop(_LOCAL_FALLBACK_SENTINEL, None))


def _route_exec_current(env: dict, session: str, task_id: Optional[str], local: bool,
                        local_sidecar: bool, route_key: str) -> tuple[dict, Optional[str], bool, int]:
    """``_route_exec`` on a copy of ``env`` -> ``(routed env, error, sidecar, generation)``, re-routed (at
    most 3 tries) when /browser connect published meanwhile; ``_reload_if_republished`` covers dispatch."""
    for _attempt in range(3):
        routed = dict(env)
        generation = _live_generation()
        route_err, sidecar = _route_exec(routed, session, task_id, local, local_sidecar, route_key)
        if route_err or _live_generation() == generation:
            break
    return routed, route_err, sidecar, generation


def _reload_if_republished(env: dict, generation: int) -> None:
    """Stop this call's daemon if the profile published a new endpoint while it spawned on the old one."""
    if _live_generation() == generation:
        return
    daemon = daemon_identity(env)
    with _driven_daemons_lock:
        _driven_daemons.pop(daemon, None)
    _reload_daemons([daemon])


def _take_real_profile_note(route_key: str) -> dict:
    """The fallback note for the first result this route returns after falling back, then nothing."""
    if route_key not in _real_profile_fallback or route_key in _real_profile_note_delivered:
        return {}
    _real_profile_note_delivered.add(route_key)
    return {"real_profile_unavailable": _real_profile_fallback[route_key]}


def browser_exec(code: str, session: str = "", timeout_s: int = _DEFAULT_TIMEOUT_S,
                 task_id: Optional[str] = None, local: bool = False):
    """Run Python code through the browser-use CLI, and return its output"""
    from agent.redact import redact_sensitive_text
    from tools.browser_use_cli_lanes import prepare_lane_call
    from tools.registry import tool_error, tool_result
    if not code or not code.strip():
        return tool_error("No code provided. Pass Python that uses the pre-imported helpers, e.g. new_tab(\"https://example.com\") then print(page_info()).")

    # The verdict is carried into the route, never re-derived: the predicate resolves DNS, so a second
    # evaluation could relax the floor and still pick the cloud for a rebinding host.
    verdict = _code_url_verdict(code)
    if verdict.blocked:
        return tool_error(verdict.blocked)
    # One endpoint per call: any private URL sends the whole call to the local sidecar. A call with no URL
    # literal stays on the route the previous call took (browser_navigate's _last_session_key twin).
    route_key = _backend_cache_key(task_id, session)
    local_sidecar = verdict.local_sidecar if _URL_RE.search(code) else _exec_route_is_sidecar.get(route_key, False)

    cmd = _find_cli()
    if not cmd:
        return tool_error("browser-harness is missing from Hermes's Python environment. "
                          "Run `hermes update` to re-sync it.")

    env = _base_subprocess_env()
    if session:
        if not _SESSION_RE.match(session):
            return tool_error(f"Invalid session name {session!r}: use 1-64 letters, digits, "
                              "dashes, or underscores (e.g. 'r7k2').")
        env["BU_NAME"] = session
    _isolate_served_profile_daemons(env)
    env, route_err, local_sidecar, generation = _route_exec_current(
        env, session, task_id, bool(local), local_sidecar, route_key)
    if route_err:
        return tool_error(route_err)
    if local_sidecar:
        env["BU_NAME"] = _sidecar_daemon_name(session)
    _exec_route_is_sidecar[route_key] = local_sidecar
    bot_desktop_browser = bool(env.pop(_BOT_DESKTOP_BROWSER_SENTINEL, None))

    lane_call = prepare_lane_call(env, code, session, task_id, local_sidecar)
    if lane_call.busy:
        return tool_error("This browser session's lane is being cleaned up after sitting idle. Retry the "
                          "same call in a minute; it will start in a fresh tab.")
    code, lane_key, browser_cfg, _ = lane_call

    workspace = _workspace_dir(task_id)
    if workspace:
        env["BH_AGENT_WORKSPACE"] = workspace

    # BU_AUTOSPAWN makes the CLI start a Browser Use cloud browser when no local
    # Chrome/CDP endpoint is reachable (their API key authenticates it)
    if "BU_AUTOSPAWN" not in env and is_legacy_browser_use_cloud_config(browser_cfg):
        env["BU_AUTOSPAWN"] = "1"

    timeout = _clamp_timeout(timeout_s)
    started = time.time()

    def dispatch() -> dict[str, Any]:
        from tools.desktop_admission import DesktopInputDenied, assert_agent_admitted
        try:
            epoch = assert_agent_admitted(env.get("BU_CDP_WS") or env.get("BU_CDP_URL") or "")
            _reconnect_desktop_harness(cmd, env, task_id, epoch)
        except DesktopInputDenied:
            return {"error_result": tool_result({"success": False, "error_type": "human_has_control",
                                                "error": "Desktop agent input is unavailable"})}
        _attach_vault_supervisor(env, task_id)
        with _driven_daemons_lock:
            daemon = daemon_identity(env)
            _driven_daemons[daemon] = hermes_home_key()
        if lane_key is not None:
            with _lane_state_lock:
                _lane_inflight[lane_key] = _lane_inflight.get(lane_key, 0) + 1
        try:
            return {"proc": _run_cli_killing_process_group(cmd, code, env, timeout)}
        except subprocess.TimeoutExpired:
            return {"error_result": tool_error(
                f"browser-use exec timed out after {timeout}s. The daemon may still be working; retry "
                f"with a larger timeout_s (max {_MAX_TIMEOUT_S}), or split the work into several calls that "
                "append to workspace files — anything already written to the workspace is preserved."
            )}
        except OSError as e:
            return {"error_result": tool_error(f"Failed to launch browser-use CLI: {e}")}
        finally:
            if lane_key is not None:
                with _lane_state_lock:
                    left = _lane_inflight.get(lane_key, 1) - 1
                    if left > 0:
                        _lane_inflight[lane_key] = left
                    else:
                        _lane_inflight.pop(lane_key, None)

    if bot_desktop_browser:
        from tools.browser_tool_session import run_fenced
        dispatched = run_fenced({"features": {"local": True}}, dispatch)
    else:
        dispatched = dispatch()
    _reload_if_republished(env, generation)
    if "proc" not in dispatched:
        if "error_result" in dispatched:
            return dispatched["error_result"]
        return tool_result(dispatched)
    proc = dispatched["proc"]

    # browser_vault fill registers injected values with this forced model-egress
    # boundary. Preserve raw stdout only for screenshot-path detection below.
    result = {
        "success": proc.returncode == 0,
        "exit_code": proc.returncode,
        "output": redact_sensitive_text(proc.stdout, force=True),
    }
    if workspace:
        result["workspace"] = workspace
    if session:
        result["session"] = session
    result.update(_take_real_profile_note(route_key))
    stderr = redact_sensitive_text((proc.stderr or "").strip(), force=True)
    if len(stderr) > _STDERR_CAP_CHARS:
        stderr = stderr[:_STDERR_CAP_CHARS] + "\n… (stderr truncated)"
    if stderr:
        result["stderr"] = stderr
    screenshot = _find_screenshot(proc.stdout, started)
    if screenshot:
        result["screenshot_path"] = screenshot
        native = _native_screenshot_result(result, screenshot)
        if native is not None:
            return native
    return tool_result(result)


_HEADER_BASE = (
    "Drive a real web browser via the Browser Use CLI: `code` runs as full Python (stdlib available) "
    "with pre-imported browser helpers; stdout comes back in the result. Start `code` with a one-line "
    "comment describing the step for the user in plain language, max 60 chars "
    "(e.g. `# Searching Amazon for paper towels`) — the UI shows it as the step label.\n\n"
    "STATE: the browser session and workspace persist across calls; Python variables do NOT (fresh "
    "interpreter each call). The workspace dir is $BH_AGENT_WORKSPACE (also `workspace` in every result); "
    "functions defined in agent_helpers.py there are auto-imported into every call. For multi-item tasks "
    "('all N products / every entry'), append each batch to a JSON/CSV file in the workspace, then read it "
    "back and aggregate in code — dedupe/count/sort with Python, not in your head — and verify the "
    "collected count against what was asked before answering.\n\n"
    "Batch each sub-procedure (navigate, wait, extract, act) into one call — do not spend a call per "
    "action — but for long extractions prefer several medium calls that append to workspace files over "
    "one giant call, so progress survives timeouts."
)

_HEADER_VISION = (
    " Screenshots are attached to your context automatically: when the exec output contains a "
    "capture_screenshot() path, the image arrives with this tool's result and you inspect it directly "
    "with your own vision — never send browser screenshots to a separate vision tool."
)

_HEADER_TEXT_ONLY = (
    " Your model cannot view images, so work text-first: page_info() for state, js() for "
    "reading/extracting DOM text, fill_input(selector, text) for inputs, and "
    "js(\"document.querySelector('…').click()\") for clicks — skip the screenshot-driven workflow described below."
)

# Appended when the local engine is Lightpanda: no graphical renderer, and one CDP
# connection holds one page — a second Target.createTarget fails with
# TargetAlreadyLoaded (drop the new_tab() sentence once lightpanda-io/browser#1962 lands).
_HEADER_LIGHTPANDA = (
    " The local engine is Lightpanda (no graphical renderer, one page per session): capture_screenshot() "
    "is unavailable, so work text-first; navigate with new_tab(url) exactly once, then goto_url(url) for "
    "every later navigation — a second new_tab() fails with TargetAlreadyLoaded."
)

# Pinned quick-reference for the CLI's pre-imported helpers, replacing the live
# ``browser-use skill`` fetch (uncontrolled third-party text in every schema: version
# drift, supply-chain exposure, byte-unstable prompt). A/B benchmarked ~equal.
_HELPERS_DIGEST = (
    "\n\nHELPERS (pre-imported): new_tab(url) opens/navigates (use for the FIRST navigation), goto_url(url) "
    "navigates the current tab, wait_for_load() after navigation, page_info() summarizes the current page "
    "state, js(expr) evaluates a JS expression and returns its value (js('document.title'); wrap function "
    "bodies as js('(() => {...})()') — a bare '() => {...}' returns the function itself, uncalled), "
    "fill_input(selector, text) types into inputs, click_at_xy(x, y) clicks viewport coordinates, "
    "capture_screenshot() saves and prints a screenshot path, cdp('Domain.method', **kwargs) is raw CDP — "
    "cdp('Accessibility.getFullAXTree')['nodes'] lists every element's role/name/backendDOMNodeId (filter "
    "in Python before printing; it is thousands of nodes), then cdp('DOM.getBoxModel', backendNodeId=n) "
    "gives click coordinates. ensure_real_tab() recovers from a stale/internal tab. Login walls: never guess "
    "credentials; see the vault note below if present, otherwise stop and ask the user."
)


def _description_header() -> str:
    """Header tailored to whether the active model can see images natively"""
    if _lazy_call("tools.browser_tool_lightpanda_fallback", "lightpanda_engine_status", (False, ""),
                  "lightpanda engine status unavailable")[0]:  # no screenshots, whatever the model sees
        return _HEADER_BASE + _HEADER_TEXT_ONLY + _HEADER_LIGHTPANDA
    vision = _lazy_call("tools.vision_tools", "_should_use_native_vision_fast_path", False, "")
    return _HEADER_BASE + (_HEADER_VISION if vision else _HEADER_TEXT_ONLY)


def _dynamic_schema_overrides() -> dict:
    overrides: dict = {"description": _description_header() + _HELPERS_DIGEST}
    # ``local`` exists ONLY when the user consented to real-profile browsing — everyone
    # else's schema carries zero extra surface. The caller memoizes on config.yaml mtime,
    # so toggling consent applies next session, not mid-chat.
    if _real_profile_consented():
        props = dict(BROWSER_EXEC_SCHEMA["parameters"]["properties"])
        props["local"] = {
            "type": "boolean", "default": False,
            "description": ("Drive the user's own local browser (a Hermes-managed copy of their real "
                            "default-Chromium profile, logins/cookies included) instead of the configured "
                            "cloud browser backend. Use when the user asks to act as themselves — their "
                            "accounts, their sessions. No-op when the backend is already local. Default false."),
        }
        overrides["parameters"] = {**BROWSER_EXEC_SCHEMA["parameters"], "properties": props}
    return overrides


BROWSER_EXEC_SCHEMA = {
    "name": "browser_exec",
    # Static fallback description, used only when the managed CLI is unavailable
    "description": (_HEADER_BASE + _HELPERS_DIGEST
                    + "\n\n(The browser-use CLI is not installed yet. Install it with `hermes tools` (Browser Automation → Browser Use).)"),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python code to execute using the pre-imported browser helpers. Use print(...) for any data you need back."},
            "session": {"type": "string", "description": "Named isolated browser session — its own daemon and (on cloud backends) own browser, so concurrent tasks don't share tabs. Reuse the same name on every related call; omit for the shared default session."},
            "timeout_s": {"type": "integer", "default": _DEFAULT_TIMEOUT_S,
                          "description": f"Max seconds to wait for the code to finish (default {_DEFAULT_TIMEOUT_S}, max {_MAX_TIMEOUT_S})."},
        },
        "required": ["code"],
    },
}


# browser_exec is additionally gated at tool-definition time — sessions whose toolsets
# lack ``terminal`` never see it (model_tools._compute_tool_definitions). check_fn only
# answers "is Browser Use mode configured"; surface policy lives with the session.
from tools.registry import registry

registry.register(
    name="browser_exec",
    toolset="browser-use",
    schema=BROWSER_EXEC_SCHEMA,
    handler=lambda args, **kw: browser_exec(
        code=args.get("code", ""), session=args.get("session", "") or "",
        timeout_s=args.get("timeout_s", _DEFAULT_TIMEOUT_S), task_id=kw.get("task_id"),
        local=bool(args.get("local", False)),
    ),
    check_fn=is_browser_use_cli_mode,
    dynamic_schema_overrides=_dynamic_schema_overrides,
    emoji="🌐",
)
