"""Owner-gated synchronous commands. No web, network or model dependencies."""
from __future__ import annotations

import json
import logging
import os
import shlex
import time

from . import board
from .summary_text import choose_locale, message, render_summary

logger = logging.getLogger(__name__)

try:
    from hermes_constants import get_hermes_home
    board.get_hermes_home = get_hermes_home
except ImportError:  # outside Hermes
    pass
try:
    from agent.secret_scope import get_secret
    board.get_secret = get_secret
except ImportError:  # outside Hermes, or a host without profile-scoped secrets
    pass


_LOCAL_SURFACES = frozenset({"", "cli", "tui", "desktop", "local"})


def _session_from_context(name: str):
    """The value the host bound for *this* call, or None when nothing was bound.

    Read the ContextVar itself. ``get_session_env`` falls back to ``os.environ``
    when a var is unset, and that environment can carry another turn's caller: a
    process started from a gateway-spawned terminal inherits it, and old hosts
    without per-call binding leave it in place. A value from there must never
    authorize anyone, so "unset" stays distinguishable from "bound to empty".
    """
    try:
        from gateway import session_context
    except ImportError:
        return None
    var = getattr(session_context, "_VAR_MAP", {}).get(name)
    unset = getattr(session_context, "_UNSET", None)
    if var is None or unset is None:
        return None
    value = var.get()
    return None if value is unset else str(value or "")


_session = _session_from_context


# Platforms whose adapter reports chat_type "dm" only for a one-to-one chat,
# checked in the host source (0.21.7 and 0.22.0): Discord (DMChannel; group DMs
# are not DMChannel), Telegram ("private"), Feishu ("p2p"), Google Chat
# (DIRECT_MESSAGE space), Mattermost (channel type "D"). Slack needs its D id.
# Everything else, including Matrix (m.direct rooms can hold more people) and
# platforms not reviewed yet, counts as shared: counts only by default.
_ONE_TO_ONE_DM = frozenset({"discord", "telegram", "feishu", "google_chat", "mattermost"})


def _private_destination(platform: str, chat_type: str, chat_id: str) -> bool:
    """Whether the reply provably lands in a one-to-one conversation."""
    if chat_type != "dm":
        return False
    if platform == "slack":
        return chat_id.startswith("D")
    return platform in _ONE_TO_ONE_DM


def _in_plugin_host() -> bool:
    """True in a ``plugins.isolation: host`` child process. The host forwards a
    command's arguments there but not the caller's session, so nothing in the
    child can say who is asking or where the reply goes."""
    return os.environ.get("HERMES_PLUGIN_HOST_PROCESS") == "1"


def _in_gateway() -> bool:
    """True only inside the running messaging gateway process.

    ``_HERMES_GATEWAY`` alone is not enough: every process that imports
    ``gateway.run`` sets it, including the ``hermes`` CLI and TUI. Like Hermes's
    own process guards, also require that this process owns the live gateway
    PID file. When the check itself fails, assume a gateway (fail closed).
    """
    if os.environ.get("_HERMES_GATEWAY") != "1":
        return False
    try:
        from gateway.status import get_running_pid
        return get_running_pid(cleanup_stale=False) == os.getpid()
    except Exception:  # health: allow BLE001 -- Pinned upstream process-identity failure must refuse unbound gateway callers.
        return True


def _arguments(raw: str):
    if len(raw) > 512:
        raise ValueError("arguments too long")
    mode, lang = "", None
    tokens = shlex.split(raw)
    for token in tokens:
        if token in {"full", "counts"}:
            mode = token
        elif token.startswith("lang="):
            lang = token[5:]
        else:
            lang = token
    return mode, lang


def _error(locale, exc):
    logger.exception("Duty Board command failed (%s)", type(exc).__name__)
    return message(locale, "error", error=type(exc).__name__)


def duty(raw_args: str = "") -> str:
    locale = "en"
    try:
        if _in_plugin_host():
            # Before reading anything: language and settings are not needed to refuse.
            return message("en", "isolated")
        settings = board._settings()
        home = board._home()
        locale = choose_locale(settings=settings, home=home)
        mode, explicit = _arguments(raw_args)
        locale = choose_locale(explicit, settings, home)
        bound_platform = _session("HERMES_SESSION_PLATFORM")
        platform = (bound_platform or "").lower()
        user_id = _session("HERMES_SESSION_USER_ID") or ""
        chat_type = (_session("HERMES_SESSION_CHAT_TYPE") or "").lower()
        # Local: no messaging surface bound for this call. In a gateway that means
        # the host did not bind the call at all (before 0.21.7), so we cannot tell
        # who is asking.
        local = platform in _LOCAL_SURFACES
        if local and bound_platform is None and _in_gateway():
            return message(locale, "oldHost")
        runtime = board._runtime_settings(settings)
        owner = runtime["owner"]
        if not local and not owner:
            return message(locale, "configureOwner")
        if not local and user_id != owner:
            return message(locale, "ownerOnly")
        full = local or _private_destination(platform, chat_type, _session("HERMES_SESSION_CHAT_ID") or "")
        if mode == "full" and not full:
            full = settings.get("chat_full_in_channels") is True
        if mode == "counts":
            full = False
        data = board.cached_summary(settings=runtime)
        text = render_summary(data, locale=locale, now=time.time(), viewer={"is_owner": True, "platform": platform, "link_style": "discord" if platform == "discord" else "plain", "counts_only": not full})
        if mode == "full" and not full:
            text += "\n" + message(locale, "dmHint")
        return text
    except Exception as exc:  # health: allow BLE001 -- Pinned upstream command boundary delegates traceback logging to _error.
        try:
            return _error(locale, exc)
        except Exception:  # health: allow BLE001 -- Pinned upstream last-resort refusal must not expose state after a broken error handler.
            # Even a damaged catalog must not escape into the gateway.
            return "Duty Board unavailable (" + type(exc).__name__ + ")."


def setup_cli(parser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--full", action="store_true", help="Show item details (default)")
    group.add_argument("--counts", action="store_true", help="Show counts only")
    parser.add_argument("--lang", metavar="L", help="Summary language")
    parser.add_argument("--json", action="store_true", help="Print raw board JSON")


def cli_handler(args) -> None:
    # A terminal command runs as the local account that can already read the
    # home. No messaging context/owner id exists in Hermes's CLI dispatch.
    locale = "en"
    try:
        settings = board._settings()
        locale = choose_locale(args.lang, settings, board._home())
        data = board.cached_summary(settings=board._runtime_settings(settings))
        if args.json:
            print(json.dumps(data, ensure_ascii=False))
        else:
            print(render_summary(data, locale=locale, now=time.time(), viewer={"is_owner": True, "platform": "cli", "link_style": "plain", "counts_only": args.counts}))
    except Exception as exc:  # health: allow BLE001 -- Pinned upstream CLI boundary delegates traceback logging to _error.
        try:
            print(_error(locale, exc))
        except Exception:  # health: allow BLE001 -- Pinned upstream last-resort CLI failure emits only the exception type.
            print("Duty Board unavailable (" + type(exc).__name__ + ").")
