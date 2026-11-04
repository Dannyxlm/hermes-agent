"""``browser_exec``: drive a real browser through the Browser Use CLI engine (browser-harness,
https://browser-use.com), Hermes' only core browser tool.

The harness speaks CDP to whatever ``tools.browser_session`` resolved for the call: an operator
``BU_CDP_*`` endpoint, a ``/browser connect`` override, a cloud provider's session, or a local browser
(real profile, Lightpanda, or the packaged Chromium started by ``tools.browser_launcher``).
"""

import contextlib
import contextvars
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

from hermes_cli.process_identity import (
    own_process_groups, register_child, release_process_group_once_empty, spawn_env, spawned_by_this_process)
from hermes_constants import get_hermes_home, hermes_home_key
from tools.browser_harness_dirs import (daemon_env, daemon_identity, fit_default_socket_dir, harness_pid_file,
                                        namespace_harness_dirs)
from utils import is_truthy_value

logger = logging.getLogger(__name__)

_BROWSER_USE_PROVIDER = "browser-use"
# The one credential the harness itself reads (BU_AUTOSPAWN of a Browser Use cloud browser).
_HARNESS_PASSTHROUGH_KEYS: tuple[str, ...] = ("BROWSER_USE_API_KEY",)

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
# Where new/hidden tabs may be brought to the front: a browser_use_shot.ACTIVATE_* mode. Popped before launch.
_CLOUD_BROWSER_SENTINEL = "_HERMES_BU_CLOUD_BROWSER"

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
# http(s) URL literals in exec code, checked against the browser URL policy
_URL_RE = re.compile(r"https?://[^\s'\"\\)]+", re.IGNORECASE)
_FHS_BIN_DIRS = ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/sbin", "/usr/local/bin", "/usr/sbin",
                 "/usr/bin", "/sbin", "/bin")


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


def _real_profile_consented() -> bool:
    return _lazy_call("tools.browser_session_cloud", "use_real_profile", False, "real-profile consent lookup failed")


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
    from tools.browser_url_policy import evaluate_url_safety, hybrid_routes_locally
    local_sidecar = False
    for url in _URL_RE.findall(code or ""):
        routes_local = hybrid_routes_locally(url)
        local_sidecar = local_sidecar or routes_local
        err = evaluate_url_safety(url, auto_local=routes_local)
        if err:
            return _CodeUrlVerdict(err.get("error", "Blocked: unsafe URL"), local_sidecar)
    return _CodeUrlVerdict(None, local_sidecar)


def _harness_credentials() -> dict:
    """The harness's own credential from the active profile's secret scope, never ``os.environ``: under
    multiplex that holds the LAUNCH profile's key, and a served profile's browser runs on its own (or none)."""
    from agent.secret_scope import current_secret_scope, get_secret, serves_routed_profile
    routed = serves_routed_profile()
    scope = (current_secret_scope() or {}) if routed else None
    values = {key: (scope.get(key) if routed else get_secret(key)) for key in _HARNESS_PASSTHROUGH_KEYS}
    return {key: value for key, value in values.items() if value is not None}


def _base_subprocess_env() -> dict:
    from tools.browser_session import build_browser_env
    env = build_browser_env()
    env.update(_harness_credentials())
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
    # A daemon the CLI starts inherits this tag; _own_harness_daemon owns only one that names us.
    env.update(spawn_env("browser-harness-daemon"))
    return env


def _floor_subprocess_path(path: str) -> str:
    """Keep system commands reachable from profile workers with a minimal PATH.

    Browser subprocesses may launch shell helpers; version-manager-only paths
    omit /usr/bin. Reuse the shared browser PATH floor on POSIX.
    """
    if os.name == "nt":
        return path
    parts = [p for p in (path or "").split(os.pathsep) if p]
    return os.pathsep.join(parts + [d for d in _FHS_BIN_DIRS if d not in set(parts) and os.path.isdir(d)])


def _read_browser_cfg() -> dict:
    """Return the ``browser:`` config section, or {} on any failure."""
    try:
        from tools.browser_session_cloud import browser_section
        return browser_section()
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


def is_legacy_browser_use_cloud_config(browser_cfg: dict) -> bool:
    """True for pre-CLI direct-API Browser Use cloud configs: no cloud_provider (or ``browser-use``), not the
    Nous gateway, and BROWSER_USE_API_KEY set; the CLI then reaches Browser Use cloud natively."""
    if not isinstance(browser_cfg, dict):
        return False
    provider = str(browser_cfg.get("cloud_provider") or "").strip().lower()
    if provider not in {"browser-use", ""} or _use_gateway(browser_cfg):
        return False
    return _browser_use_api_key_set()


def _browser_use_api_key_set() -> bool:
    """BROWSER_USE_API_KEY for the served profile: the only credential Hermes autospawns BU cloud with.
    Profile-scoped so a multiplexed secondary never inherits the default's cloud mode."""
    from agent.secret_scope import get_secret
    return bool(get_secret("BROWSER_USE_API_KEY", ""))


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


def browser_exec_available() -> bool:
    """check_fn: browser-harness is importable (a core dependency). Never launches or opens sockets."""
    return _find_cli() is not None


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
        # same policy as the vision_analyze native embed.
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
    from tools.browser_session import real_profile_note
    note = real_profile_note(key)
    if note:
        env.setdefault(_REAL_PROFILE_FALLBACK_SENTINEL, note)


def _mark_local_browser(env: dict, session: dict, key: str) -> None:
    """Flags for a LOCAL session browser: it runs on the Bot Desktop display (lease fence), and unless a
    persistent profile / the real-profile browser is shared by every key, it is exclusive to this key (no
    own-tab preamble: nothing to share a tab with)."""
    features = session.get("features") or {}
    env[_BOT_DESKTOP_BROWSER_SENTINEL] = "lightpanda" if features.get("lightpanda") else "local"
    if not session.get("shared_profile") and not features.get("real_profile"):
        env[_PRIVATE_BROWSER_SENTINEL] = "1"
    _export_real_profile_note(env, key)


def _resolve_local_cdp(env: dict, task_id: Optional[str], session_name: str = "",
                       sidecar: bool = False) -> Optional[str]:
    """Local browser for this cache key through the session layer: the consented real profile, Lightpanda
    (``browser.engine: lightpanda``), or the packaged Chromium. Never the harness's own discovery of the
    user's installed Chrome (chrome://inspect toggle + Allow popup per run, blocked on Chrome >=136)."""
    from tools.browser_lightpanda import using_lightpanda_engine
    from tools.browser_session import get_session_info
    key = _backend_cache_key(task_id, session_name, sidecar)
    session: dict = {}

    def _session_info(cache_key: str) -> dict:
        session.update(get_session_info(cache_key) or {})
        _stamp_real_profile_session(session)
        return session

    if _quiet(using_lightpanda_engine, False):
        fail = lambda e: (f"Lightpanda could not be started: {e} Set browser.engine to auto "
                          "to use local Chrome, or switch backends via `hermes tools` → Browser Automation.")
    else:
        fail = lambda e: (f"The local browser could not be started: {e} "
                          "Run `hermes tools` → Browser Automation to (re)install Chromium, or switch backends.")
    err = _export_session_cdp(env, _session_info, key, fail, "The local browser session returned no CDP endpoint.")
    if err is None:
        _mark_local_browser(env, session, key)
    return err


def _resolve_backend_cdp(env: dict, task_id: Optional[str], session_name: str = "",
                         force_local_sidecar: bool = False) -> Optional[str]:
    """Point the harness at the configured backend's CDP endpoint; error string on failure.

    Precedence: (1) ``BU_CDP_WS``/``BU_CDP_URL`` already in env (operator override); (2) ``BROWSER_CDP_URL``
    env / ``browser.cdp_url`` (``/browser connect``); (3) a cloud provider via ``browser_session.get_session_info``
    (per-key cache, expiry, reaper, atexit; a provider failure falls back to a local browser);
    (4) the local engine — ``browser.engine: lightpanda`` or Hermes' packaged Chromium via the launcher
    (never the harness's own discovery of the user's installed Chrome); (5) BU direct-API configs WITH
    ``BROWSER_USE_API_KEY`` → None (checked before (3)): the CLI reaches BU cloud natively (BU_AUTOSPAWN); a
    keyless ``browser-use`` provider (managed-gateway autodetect, or a selection missing its key) takes (3).
    ``session_name`` (BU_NAME) keys the session cache so each name gets its OWN browser — what makes named
    sessions concurrent-safe.

    ``force_local_sidecar`` skips step (3): the call runs on the local engine under the ``::local`` cache
    key (hybrid routing; see ``browser_url_policy.hybrid_routes_locally``). It sits BELOW the two operator overrides, and the
    sidecar verdict never fires while a CDP override is set."""
    if _has_cdp_env(env):
        env[_SHARED_BROWSER_SENTINEL] = "1"  # operator BU_CDP_* or the real-profile browser
        return None
    from tools.browser_session import get_session_info
    from tools.browser_session_cdp import get_cdp_override
    from tools.browser_session_cloud import get_cloud_provider
    override = _quiet(get_cdp_override, "")
    if override:
        _set_cdp_env(env, override)
        env[_SHARED_BROWSER_SENTINEL] = "1"
        return None
    if force_local_sidecar:
        return _resolve_local_cdp(env, task_id, session_name, sidecar=True)
    provider = _quiet(get_cloud_provider, None, "Cloud provider lookup failed")
    if provider is None:
        return _resolve_local_cdp(env, task_id, session_name)

    # Browser Use direct-API configs: the CLI talks to BU cloud natively (BU_AUTOSPAWN) — the
    # legacy provider would create a second, redundant session. Nous-gateway configs (cloud_provider: nous
    # from the picker, or the pre-picker use_gateway: true) DO resolve through the provider: the gateway
    # provisions the browser server-side and returns its CDP URL. Without BROWSER_USE_API_KEY the CLI cannot
    # autospawn, and the provider is either the managed gateway (autodetect on a never-configured install) or
    # a selection missing its key; both go through the session path (gateway CDP, or the session layer's
    # local-Chromium fallback) instead of leaving the harness to hunt for an installed Chrome.
    provider_key = str(getattr(provider, "name", "") or "").strip().lower()
    if provider_key == _BROWSER_USE_PROVIDER and not _use_gateway(_read_browser_cfg()) and _browser_use_api_key_set():
        env[_PRIVATE_BROWSER_SENTINEL] = "1"  # named BU cloud browsers are exclusive to their daemon
        # Only intent: the harness attaches to a local Chrome already listening instead of autospawning, so
        # the live daemon's own report decides.
        from tools.browser_use_shot import ACTIVATE_IF_CLOUD
        env[_CLOUD_BROWSER_SENTINEL] = ACTIVATE_IF_CLOUD
        return None

    provider_name = type(provider).__name__
    session: dict = {}

    def _session_info(key: str) -> dict:
        session.update(get_session_info(key) or {})
        _stamp_real_profile_session(session)
        return session

    key = _backend_cache_key(task_id, session_name)
    err = _export_session_cdp(
        env, _session_info, key,
        lambda e: (f"Cloud browser provider {provider_name} failed to provide a session: {e}. "
                   "Fix the provider configuration or switch backends via `hermes tools` → Browser Automation."),
        f"Cloud browser provider {provider_name} returned no CDP endpoint, so browser_exec "
        "cannot drive it. Pick a CDP-capable provider via `hermes tools` → Browser Automation.",
    )
    if session.get("fallback_from_cloud"):
        # The provider failed (rate limit, quota, outage) and the session layer fell back to a LOCAL
        # browser for this key; it already carries its CDP URL.
        logger.warning("browser_exec: %s failed (%s); using local Chromium for this session",
                       provider_name, session.get("fallback_reason"))
        if err is None:
            _mark_local_browser(env, session, key)
        return err
    if err is None:
        from tools.browser_use_shot import ACTIVATE_ALWAYS
        env[_CLOUD_BROWSER_SENTINEL] = ACTIVATE_ALWAYS  # the provider's own session: the harness connects only there
    # A provider browser keyed bu-named-<name> is exclusive to this session — the
    # own-tab preamble would just leak a blank tab into it.
    if err is None and session_name:
        env[_PRIVATE_BROWSER_SENTINEL] = "1"
    return err


# Source identity supplements upstream's per-home launcher key: changing the pinned Chromium
# profile in one home must not reuse its old account, and consent-off must evict cached sessions too.
_real_profile_scopes: dict[str, dict] = {}
_real_profile_task_homes: dict[str, str] = {}
_real_profile_scope_lock = threading.RLock()


def _real_profile_scope() -> tuple[Optional[dict], Optional[str]]:
    from hermes_cli.browser_connect import (
        _resolve_source_profile, detect_default_chromium, real_profile_copy_dir, real_profile_data_dir)
    from tools.browser_session_real_profile import _unsupported_reason
    import hashlib

    browser = detect_default_chromium()
    unsupported = _unsupported_reason(browser)
    if unsupported:
        return None, unsupported
    source_dir = real_profile_data_dir(browser)
    if not source_dir:
        return None, f"browser.use_real_profile is on, but no profile directory exists for '{browser}'."
    source_profile, error = _resolve_source_profile(source_dir)
    if error or not source_profile:
        return None, f"browser.use_real_profile is on, but {error or 'no source profile was resolved'}."
    copy_dir = real_profile_copy_dir(browser)
    material = "\0".join((os.path.realpath(copy_dir), browser, os.path.realpath(source_dir), source_profile))
    return {"key": hashlib.sha256(material.encode("utf-8")).hexdigest()[:16],
            "copy_root": os.path.realpath(str(get_hermes_home() / "browser-profile")),
            "copy_dir": os.path.realpath(copy_dir), "source_profile": source_profile}, None


def _revoke_real_profile_for_current_home(*, delete_snapshots: bool = True) -> None:
    """Forget only this home's copy-browser, cached sessions, supervisors and harness daemons."""
    from tools import browser_session as bs
    from tools.browser_session_lifecycle import cleanup_single_session
    from tools.browser_session_real_profile import _consent_revoked, terminate_real_profile_browser
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    home = hermes_home_key()
    with _real_profile_scope_lock:
        _real_profile_scopes.pop(home, None)
        with bs._cleanup_lock:
            keys = [key for key, info in bs._active_sessions.items()
                    if info.get("home_key") == home and (info.get("features") or {}).get("real_profile")]
        for key in keys:
            cleanup_single_session(key)
        tasks = [task for task, owner in _real_profile_task_homes.items() if owner == home]
        if keys or tasks:
            SUPERVISOR_REGISTRY.stop_home(home)
        for task in tasks:
            _real_profile_task_homes.pop(task, None)
        # Stop latching harness sockets before removing the copy's credential files.
        if keys or tasks:
            stop_harness_daemons(home)
        if delete_snapshots:
            _consent_revoked()
        else:
            terminate_real_profile_browser()


def _check_real_profile_scope() -> Optional[str]:
    """Reconcile consent/source identity before a cached session or supervisor can be reused."""
    from tools.browser_launcher import get_browser
    from tools.browser_session_real_profile import real_profile_key

    with _real_profile_scope_lock:
        if not _real_profile_consented():
            _revoke_real_profile_for_current_home()
            return None
        scope, error = _real_profile_scope()
        home = hermes_home_key()
        previous = _real_profile_scopes.get(home)
        running = get_browser(real_profile_key())
        changed = previous is not None and (scope is None or previous["key"] != scope["key"])
        wrong_copy = running is not None and (scope is None or
                     os.path.realpath(running.user_data_dir) != scope["copy_dir"])
        if changed or wrong_copy:
            _revoke_real_profile_for_current_home(delete_snapshots=False)
            if running is not None and not running.owned:
                return ("The real-profile source changed while another process holds the old copy-browser. "
                        "Close that copy-browser before retrying; its account was not reused.")
        if scope is not None:
            _real_profile_scopes[home] = scope
        return error


def _stamp_real_profile_session(session: dict) -> None:
    if (session.get("features") or {}).get("real_profile"):
        scope = _real_profile_scopes.get(hermes_home_key()) or {}
        session["features"].update(real_profile_scope=scope.get("key"),
                                   real_profile_copy_root=scope.get("copy_root"))


def _resolve_real_profile_cdp(env: dict, force_local: bool) -> Optional[str]:
    """Point the harness at the user's real-profile copy-browser (a SNAPSHOT of their default Chromium
    profile, hermes_cli.browser_connect) when consented. Two ways in: the effective backend is already local
    (no provider, CDP override, or legacy BU cloud config) → silent upgrade; or ``force_local`` (consent-gated
    ``local`` arg) → the user's browser even under a cloud backend. Operator overrides (BU_CDP_* env,
    /browser connect, ``browser.cdp_url``) own the session either way. A launch error is returned;
    ``_route_backend`` then falls back to the normal backend and reports it, never silently."""
    if not _real_profile_consented():
        _revoke_real_profile_for_current_home()
        return None
    if _has_cdp_env(env):
        return None
    from tools.browser_session_cdp import get_cdp_override_raw
    from tools.browser_session_cloud import get_cloud_provider
    from tools.browser_session_real_profile import real_profile_cdp
    if _quiet(get_cdp_override_raw, ""):
        return None
    # Only auto-upgrade genuinely-local attaches; any cloud path (provider, provider lookup failure, or
    # legacy BU cloud config) stays on its backend unless the model passes local=true.
    if not force_local and (_quiet(get_cloud_provider, object()) is not None
                            or is_legacy_browser_use_cloud_config(_read_browser_cfg())):
        return None
    scope_error = _check_real_profile_scope()
    if scope_error:
        return scope_error
    cdp, err = real_profile_cdp()
    if cdp and not err:
        _set_cdp_env(env, cdp)
        env[_BOT_DESKTOP_BROWSER_SENTINEL] = "local"  # the user's real Chromium, whatever browser.engine says
        env[_SHARED_BROWSER_SENTINEL] = "1"
    return err or None


def _attach_vault_supervisor(env: dict, task_id: Optional[str]) -> None:
    """Attach the per-task CDP supervisor to the browser this exec drives so ``browser_vault`` fill has
    a secret-capable WebSocket (never argv) into the SAME browser. Only CDP-routed backends expose an
    endpoint; BU direct-cloud (BU_AUTOSPAWN) does not, and the vault tools report ``supervisor_required``."""
    cdp = env.get("BU_CDP_WS") or env.get("BU_CDP_URL")
    if not cdp:
        return
    try:
        from tools.browser_session_cdp import get_dialog_policy_config, resolve_cdp_override
        from tools.browser_supervisor import SUPERVISOR_REGISTRY
        policy, timeout_s = get_dialog_policy_config()
        SUPERVISOR_REGISTRY.get_or_start(task_id=task_id or "default", cdp_url=resolve_cdp_override(cdp),
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
    # layer makes the one real-profile-or-fresh decision and reports it (_resolve_local_cdp).
    if not _has_cdp_env(env) and (rp_err or (local and not real_profile)):
        if rp_err:
            from tools.browser_session_real_profile import real_profile_fallback_note
            env[_REAL_PROFILE_FALLBACK_SENTINEL] = real_profile_fallback_note(rp_err)
        env[_LOCAL_FALLBACK_SENTINEL] = "1"
        force_local_sidecar = True
    return _resolve_backend_cdp(env, task_id, session_name=session,
                                force_local_sidecar=force_local_sidecar)


def _group_popen_kwargs() -> dict:
    """Popen kwargs starting the CLI in its own process group (a new session on POSIX) so a
    timeout can take down every process that inherited the capture pipes, not just the CLI
    child. Windows also hides the console window."""
    def _flags() -> dict:
        from hermes_cli._subprocess_compat import windows_hide_flags
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return {"creationflags": windows_hide_flags() | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                "startupinfo": si}
    # detached-spawn: ok — awaited CLI; a timeout kills its whole group (_run_cli_killing_process_group)
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


# How often a running browser_exec re-checks for the daemon its first call starts.
_OWN_DURING_CALL_S = 0.5
# Harness daemons this process drove: each latches BU_CDP_* at start and outlives every call, so a backend
# swap (/browser connect|disconnect) leaves later calls in the old browser until these are stopped.
_driven_daemons: dict[tuple[str, ...], str] = {}  # daemon identity -> home
_driven_daemons_lock = threading.Lock()
_desktop_daemon_epochs: dict[tuple[str, str], int] = {}
_live_generation = functools.partial(_lazy_call, "tools.browser_session_cdp", "live_cdp_generation", 0,
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


def _published_daemon_pid(env: dict) -> Optional[int]:
    """The pid the harness published for the daemon ``env`` drives (``harness_pid_file``: JSON
    ``{"pid", "started"}`` or a bare pid), or None when there is none."""
    try:
        raw = harness_pid_file(env).read_text(encoding="utf-8-sig").strip()
        return int(json.loads(raw)["pid"]) if raw.startswith("{") else int(raw.split()[0])
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def _own_harness_daemon_until_owned(env: dict) -> Optional[bool]:
    """Periodic check while browser_exec runs: own the daemon as soon as it publishes its pid, then
    stop (False). It also stops once the daemon can never be ours (Windows, a managed worker, a daemon
    another Hermes started), instead of re-checking every tick for the whole call."""
    return None if _own_harness_daemon(env) is None else False


def _reload_daemons(daemons) -> None:  # each ``daemon_identity`` daemon, with its own dirs
    cmd = _find_cli() if daemons else None
    if not cmd:
        return
    env = _base_subprocess_env()
    for daemon in daemons:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            _run_cli_killing_process_group([*cmd, "--reload"], "", daemon_env(env, daemon), 15)
        with _owned_daemon_groups_lock:
            pgid = _owned_daemon_groups.pop(daemon, None)
        if pgid is not None:
            release_process_group_once_empty(pgid)  # a daemon --reload failed to stop stays covered


# Daemon identity (``daemon_identity``: its dirs + BU_NAME) -> process group of the harness daemon this
# process owns. The harness starts its daemon with start_new_session so it outlives each CLI call (that
# is how a session persists across browser_exec calls), which also means it outlives a SIGKILLed or
# OOM-killed Hermes forever (#121095). Its group goes to this process's owner-death supervisor, so it
# dies with us however we die.
_owned_daemon_groups: dict[tuple[str, ...], int] = {}
_owned_daemon_groups_lock = threading.Lock()


def _own_harness_daemon(env: dict) -> Optional[bool]:
    """Hand the daemon behind ``env``'s BU_NAME to the owner-death supervisor.

    The harness publishes its daemon pid in its runtime dir (``harness_pid_file``: JSON
    ``{"pid", "started"}`` or a bare pid). The daemon is accepted only while it is this user's live
    ``browser_harness.daemon`` leading its own session and its spawn tag names this process, so a
    stale or planted pid file, or a daemon another Hermes started, never reaches the supervisor.
    Runs after every browser_exec (and while a long one runs, until the daemon is owned): a pid
    already owned for this daemon costs one file read.

    Returns True once the daemon is owned, False when it can never be (Windows, a managed worker, a
    daemon another Hermes started), None when there is nothing to own yet."""
    if os.name != "posix":
        return False  # Windows: the harness daemon is outside this process's job; nothing to register.
    from agent.runtime_session_store import is_worker_process
    if is_worker_process():
        # A managed per-turn worker never owns it, so a killed worker does not take the browser the
        # next turn's worker drives along (its clean exit still runs the atexit browser cleanup).
        return False
    import psutil

    daemon = daemon_identity(env)
    pid = _published_daemon_pid(env)
    if pid is None:
        logger.debug("browser_exec: no harness daemon pid published for %s", daemon[-1])
        return None
    try:
        with _owned_daemon_groups_lock:
            if _owned_daemon_groups.get(daemon) == pid:
                return True
        proc = psutil.Process(pid)
        argv = proc.cmdline()
        if (os.getsid(pid) != pid or proc.uids().real != os.getuid()  # windows-footgun: ok — posix-only, returned above
                or argv[argv.index("-m") + 1] != "browser_harness.daemon"):
            return None
        if not spawned_by_this_process(pid):
            return False  # another Hermes on the same BU_NAME started it: that Hermes owns it
    except (OSError, ValueError, IndexError, psutil.Error) as exc:
        logger.debug("browser_exec: no harness daemon to supervise for %s: %s", daemon[-1], exc)
        return None
    with _owned_daemon_groups_lock:
        replaced = _owned_daemon_groups.get(daemon)
        _owned_daemon_groups[daemon] = pid
    own_process_groups([pid])
    register_child(pid, "browser-harness-daemon")
    if replaced is not None:
        release_process_group_once_empty(replaced, timeout=0)  # the daemon it replaced; a survivor stays covered
    return True


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


def _remember_real_profile_task(env, task_id):
    """Attribute a task only when its resolved endpoint is the running real-profile browser."""
    if _has_cdp_env(env) and hermes_home_key() in _real_profile_scopes:
        from tools.browser_launcher import get_browser
        from tools.browser_session_real_profile import real_profile_key
        running = get_browser(real_profile_key())
        if running is not None and (env.get("BU_CDP_WS") or env.get("BU_CDP_URL")) == running.cdp_url:
            _real_profile_task_homes[task_id or "default"] = hermes_home_key()


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
    # literal stays on the route the previous call took.
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
    fit_default_socket_dir(env)  # BU_NAME is final: a deep HOME's socket would overflow sun_path
    _exec_route_is_sidecar[route_key] = local_sidecar
    _remember_real_profile_task(env, task_id)
    local_engine = env.pop(_BOT_DESKTOP_BROWSER_SENTINEL, None)
    bot_desktop_browser = bool(local_engine)
    _exec_local_engine.set(local_engine)  # only local browsers carry the sentinel

    lane_call = prepare_lane_call(env, code, session, task_id, local_sidecar)
    if lane_call.busy:
        return tool_error("This browser session's lane is being cleaned up after sitting idle. Retry the "
                          "same call in a minute; it will start in a fresh tab.")
    code, lane_key, browser_cfg, _ = lane_call
    # Every backend: capture_screenshot() falls back to jpeg instead of hanging on a png a hidden tab never
    # renders; only on a cloud browser does it (and new_tab) bring the tab to the front (tools/browser_use_shot.py).
    from tools.browser_use_shot import ACTIVATE_NEVER, harness_command
    cmd = harness_command(cmd, activate=env.pop(_CLOUD_BROWSER_SENTINEL, None) or ACTIVATE_NEVER)

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
        # The first call starts the daemon and may run long (navigation, user code): own it as soon as
        # it publishes its pid, so a Hermes killed mid-call takes it along (the call's end owns it too).
        from agent.periodic_scheduler import schedule
        owning = schedule(lambda: _own_harness_daemon_until_owned(env), _OWN_DURING_CALL_S)
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
            owning.cancel(wait=5)  # an in-flight check finishes before the post-call one runs

    if bot_desktop_browser:
        from tools.browser_session import run_fenced
        dispatched = run_fenced({"features": {"local": True}}, dispatch)
    else:
        dispatched = dispatch()
    _reload_if_republished(env, generation)
    _own_harness_daemon(env)
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
    "Drive a real browser for JS pages, logins, forms and clicks (Browser Use CLI). `code` runs as full "
    "Python (stdlib available) with pre-imported browser helpers; stdout comes back in the result. Start `code` with a one-line "
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

_TEXT_FIRST = (
    "page_info() for state, js() for reading/extracting DOM text, fill_input(selector, text) for inputs, and "
    "js(\"document.querySelector('…').click()\") for clicks."
)

# The non-native header states a transport fact (screenshots do not ride back inside THIS
# tool's results: routing is not ``native`` — e.g. an explicit ``auxiliary.vision`` pin — or
# the route rejects tool-result media), never a verdict on the model's own vision: models
# quote "your model cannot view images" against themselves and skip user images (#131568).
# model_tools.get_tool_definitions() appends the vision_analyze pointer after
# SCREENSHOTS_DETACHED when the session actually has that tool.
SCREENSHOTS_DETACHED = " Screenshots are not attached to this tool's results."

_HEADER_TEXT_ONLY = SCREENSHOTS_DETACHED + " Work text-first: " + _TEXT_FIRST

# Lightpanda: no graphical renderer, and one CDP connection holds one page — a second
# Target.createTarget fails with TargetAlreadyLoaded (drop the new_tab() sentence once
# lightpanda-io/browser#1962 lands).
_HEADER_LIGHTPANDA = (
    " The local engine is Lightpanda (no graphical renderer, one page per session): capture_screenshot() "
    "is unavailable, so work text-first: " + _TEXT_FIRST + " Navigate with new_tab(url) exactly once, then "
    "goto_url(url) for every later navigation — a second new_tab() fails with TargetAlreadyLoaded."
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
    """Header tailored to whether screenshots ride back natively in this tool's results."""
    if _lazy_call("tools.browser_lightpanda", "lightpanda_engine_status", (False, ""),
                  "lightpanda engine status unavailable")[0]:  # no screenshots, whatever the model sees
        return _HEADER_BASE + _HEADER_LIGHTPANDA
    if _lazy_call("tools.vision_tools", "_should_use_native_vision_fast_path", False, ""):
        return _HEADER_BASE + _HEADER_VISION
    return _HEADER_BASE + _HEADER_TEXT_ONLY


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
# answers "is browser-harness importable"; surface policy lives with the session.
from tools.registry import registry


# The LOCAL browser this context's browser_exec call ran on ("local" Chromium or "lightpanda", from its session),
# else None: a configured cloud provider did not serve a hybrid ::local sidecar, cloud-fallback or real-profile
# call, and browser.engine read afterwards may name another engine than the session's, so neither labels it.
_exec_local_engine: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("browser_exec_local_engine",
                                                                                   default=None)


def _handle_browser_exec(args: dict, **kw):
    """Registry handler; each call counts against the backend that served it (shared metrics, opt-in)."""
    from hermes_cli.observability.shared_metrics_loop import record_browser_call
    from tools.browser_session_cloud import browser_backend_name
    token = _exec_local_engine.set(None)
    try:
        return record_browser_call(lambda: browser_exec(
            code=args.get("code", ""), session=args.get("session", "") or "",
            timeout_s=args.get("timeout_s", _DEFAULT_TIMEOUT_S), task_id=kw.get("task_id"),
            local=bool(args.get("local", False)),
        ), lambda: browser_backend_name(local_engine=_exec_local_engine.get()))
    finally:
        _exec_local_engine.reset(token)


registry.register(
    name="browser_exec",
    toolset="browser-use",
    schema=BROWSER_EXEC_SCHEMA,
    handler=_handle_browser_exec,
    check_fn=browser_exec_available,
    dynamic_schema_overrides=_dynamic_schema_overrides,
    emoji="🌐",
)
