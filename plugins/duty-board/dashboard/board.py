"""Duty Board model-free, read-only collectors (no web dependencies).

The plugin is intentionally read-only against Hermes state.  It writes only its
own dismissal file below ``<HERMES_HOME>/duty-board``.

Optional ``duty-board/settings.json`` keys:

* ``owner`` — platform user id whose conversations count as "mine".  When it is
  absent every conversation counts as mine and nothing is hidden.  The
  ``DUTY_BOARD_OWNER`` environment variable overrides this value.
* ``hide_other_people_content`` — default ``true`` when an owner is set.
* ``people`` — map of platform user ids to display labels.
* ``window_days``, ``stall_seconds``, ``unanswered_grace_seconds``,
  ``recent_done_hours``, ``goal_lookback_days`` and ``messaging_sources`` —
  tunables, with the defaults declared below.  Values are clamped to safe
  ranges before use.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

# Optional host home resolver is supplied by adapters, keeping this module
# strictly stdlib-only while preserving context-local Hermes homes.
get_hermes_home: Optional[Callable[[], Path]] = None
# Optional host secret resolver, ``get_secret(name, default)``. Hermes scopes
# ``.env`` values to the routed profile; without it, ``os.environ`` is the source.
get_secret: Optional[Callable[..., Optional[str]]] = None


__version__ = "0.4.0"
logger = logging.getLogger(__name__)

DEFAULT_MESSAGING_SOURCES = (
    "discord", "telegram", "slack", "whatsapp", "whatsapp_cloud", "signal", "mattermost", "matrix",
    "feishu", "dingtalk", "wecom", "weixin", "wechat", "wechat_android", "qqbot", "bluebubbles",
    "email", "sms", "yuanbao",
)
DEFAULTS = {
    "window_days": 7,
    "stall_seconds": 600,
    "unanswered_grace_seconds": 180,
    "recent_done_hours": 24,
    "goal_lookback_days": 14,
    "messaging_sources": DEFAULT_MESSAGING_SOURCES,
}
CACHE_TTL_S = 4.0
DROPPED_LOOKBACK_DAYS = 7
SCAN_LIMIT_MSGS = 40

# Messages stored with role=user that were not typed by a person.
_INTERNAL_PREFIXES = (
    "[IMPORTANT:", "[ASYNC DELEGATION", "[CONTEXT COMPACTION", "[Gateway internal event",
    "[Your active task list", "[System:", "[Cron delivery", "[Note:", "[SYSTEM:",
)
_META_PREFIX_RE = re.compile(
    r"^\s*(?:\[(?:Verified sender|Untrusted sender|Triggering message id|Replying to|Voice channel)[^\]]*\]\s*)+",
)
_ORIGIN_BLOCK_RE = re.compile(
    r"^\s*Gateway message origin \(JSON data[^\n]*\n\{.*?\}\n(?:Do not guess[^\n]*\n)?\s*", re.S
)
# Only the meaningful tail is tested.  URL question marks, mid-report
# questions, MEDIA lines, and footnotes therefore do not turn a report into a
# waiting card.
_ASK_RE = re.compile(
    r"？|؟|\?(?=\s|$|[*_」”』）)\]»«“’\"'])|要不要我|要不要先|需不需要我|是否需要|你定|你来定|拍板|请确认|你确认|确认要|"
    r"等你|说一声|告诉我|回一句|你选|还是你|可以吗|好吗|行吗|你看呢|你觉得|"
    r"should I|do you want|let me know|your call|confirm|shall I|which one|would you like|"
    r"しますか|よろしいですか|教えてください|どうしますか|確認してください",
    re.I,
)
_PROMISE_RE = re.compile(
    r"我会|稍后|到时候|完成后|跑完|做完|写完|再告诉你|再来跟你说|再汇报|回来告诉你|随后|"
    r"I'll|I will|will report back|later|when it's done|I'll let you know|"
    r"後で|完了したら|報告します",
    re.I,
)
_NOISE_LINE_RE = re.compile(
    r"^(?:MEDIA:|https?://|<https?://|\[\d+\]|\[\^\d+\]:|_.*_$|`[^`]*`$|-{3,}$)",
    re.I,
)
_URL_RE = re.compile(r"<?https?://\S+>?", re.I)
_DIGITS_RE = re.compile(r"^[0-9]{1,25}$")

_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {"at": 0.0, "data": None, "home": None}


# ── home, settings, schema ────────────────────────────────────────────────


def _home() -> Path:
    """The Hermes home this call belongs to.

    Ask the host first: ``get_hermes_home`` honours the context-local override a
    multi-profile gateway sets for the routed profile, then ``HERMES_HOME``. Reading
    ``HERMES_HOME`` first would answer with the launch profile, and so with the
    wrong owner, settings and data. Fall back to the environment only outside a
    Hermes runtime.
    """
    if get_hermes_home is not None:
        try:
            return Path(get_hermes_home())
        except Exception:
            logger.debug("Hermes home lookup failed; using HERMES_HOME or ~/.hermes", exc_info=True)
    override = os.environ.get("HERMES_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes"  # health: allow HX001 -- Pinned standalone fallback; Hermes adapters inject the profile-aware resolver.


def _state_dir() -> Path:
    directory = _home() / "duty-board"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # health: allow BLE001 -- Pinned upstream optional JSON reader degrades to a bounded default.
        return default


def _settings() -> Dict[str, Any]:
    raw = _load_json(_home() / "duty-board" / "settings.json", {})
    return raw if isinstance(raw, dict) else {}


def _clamp_number(raw: Any, default: float, minimum: float, maximum: float, integer: bool = False) -> Any:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    value = max(minimum, min(maximum, value))
    return int(value) if integer else value


def _strict_bool(raw: Any, default: bool) -> bool:
    return raw if isinstance(raw, bool) else default


def _owner_override() -> Optional[str]:
    """``DUTY_BOARD_OWNER`` for the profile this call serves.

    In a multi-profile gateway the launch environment belongs to the launch
    profile; the host's secret scope answers for the routed one (``None`` when
    that profile does not set it). Any failure there, including an unscoped read
    while multiplexing, means "no override": settings.json decides.
    """
    if get_secret is not None:
        try:
            return get_secret("DUTY_BOARD_OWNER", None)
        except Exception:
            logger.debug("Scoped DUTY_BOARD_OWNER lookup failed; using settings.json", exc_info=True)
            return None
    return os.environ.get("DUTY_BOARD_OWNER")


def _runtime_settings(raw=None) -> Dict[str, Any]:
    raw = _settings() if raw is None else raw
    env_owner = _owner_override()
    owner = env_owner if env_owner is not None else raw.get("owner")
    owner = str(owner).strip() if owner is not None else ""
    sources = raw.get("messaging_sources", DEFAULTS["messaging_sources"])
    if isinstance(sources, str):
        sources = [sources]
    if not isinstance(sources, (list, tuple)):
        sources = DEFAULTS["messaging_sources"]
    sources = tuple(str(source).strip() for source in sources if str(source).strip())
    if not sources:
        sources = DEFAULTS["messaging_sources"]
    return {
        "owner": owner,
        "hide_other_people_content": _strict_bool(raw.get("hide_other_people_content"), True),
        "people": {str(k): str(v) for k, v in (raw.get("people") or {}).items()} if isinstance(raw.get("people"), dict) else {},
        "window_days": _clamp_number(raw.get("window_days"), 7, 1, 365, integer=True),
        "stall_seconds": _clamp_number(raw.get("stall_seconds"), 600, 30, 7 * 86400),
        "unanswered_grace_seconds": _clamp_number(raw.get("unanswered_grace_seconds"), 180, 0, 7 * 86400),
        "recent_done_hours": _clamp_number(raw.get("recent_done_hours"), 24, 1, 30 * 24, integer=True),
        "goal_lookback_days": _clamp_number(raw.get("goal_lookback_days"), 14, 1, 365, integer=True),
        "messaging_sources": sources,
    }


def _is_owner(user_id: Any, settings: Optional[Dict[str, Any]] = None) -> bool:
    settings = settings or _runtime_settings()
    owner = str(settings.get("owner") or "")
    if not owner or not settings.get("hide_other_people_content", True):
        return True
    return str(user_id or "") == owner


def _schema_snapshot(conn: sqlite3.Connection) -> Dict[str, Set[str]]:
    """Read the private Hermes schema once for this request."""
    result: Dict[str, Set[str]] = {}
    for table in ("sessions", "messages", "session_turn_leases", "async_delegations", "state_meta"):
        try:
            result[table] = {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
        except sqlite3.Error:
            result[table] = set()
    return result


def _missing(schema: Dict[str, Set[str]], table: str, columns: Iterable[str]) -> List[Dict[str, str]]:
    available = schema.get(table, set())
    if not available:
        return [{"code": "missingTable", "detail": table}]
    return [{"code": "missingColumn", "detail": f"{table}.{column}"} for column in columns if column not in available]


_ISSUE_TEXT = {
    "missingTable": "table {detail} not found",
    "missingColumn": "column {detail} not found",
    "collectorFailed": "collector failed ({detail})",
}


class _Issues(list):
    """Structured section problems; the UI localises them from ``code``."""

    def add(self, section: str, issue: Dict[str, str]) -> None:
        item = {"section": section, "code": issue["code"], "detail": str(issue.get("detail", ""))}
        if item not in self:
            self.append(item)

    def as_text(self) -> List[str]:
        return [f"{i['section']}: " + _ISSUE_TEXT[i["code"]].format(detail=i["detail"]) for i in self]


def _warn(warnings: List[Dict[str, str]], section: str, issue: Dict[str, str]) -> None:
    if isinstance(warnings, _Issues):
        warnings.add(section, issue)
        return
    item = {"section": section, "code": issue["code"], "detail": str(issue.get("detail", ""))}
    if item not in warnings:
        warnings.append(item)


def _connect() -> sqlite3.Connection:
    db_path = _home() / "state.db"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


def _dismissed() -> Dict[str, Any]:
    return _load_json(_home() / "duty-board" / "dismissed.json", {})


def _row_get(row: Optional[sqlite3.Row], key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


# ── text and Discord helpers ──────────────────────────────────────────────


def _text(content: Any) -> str:
    if content is None:
        return ""
    value = str(content)
    if value.startswith('[{"') or value.startswith("[{'"):
        try:
            parts = json.loads(value)
            value = " ".join(p.get("text", "") for p in parts if isinstance(p, dict))
        except Exception:  # health: allow BLE001, S110 -- Pinned upstream display parser keeps malformed text as text.
            pass
    return value


def _is_internal(content: str, display_kind: Optional[str]) -> bool:
    if display_kind in ("internal_notification", "hidden"):
        return True
    return content.lstrip()[:80].startswith(_INTERNAL_PREFIXES)


def _clean_user(content: str) -> str:
    value = _ORIGIN_BLOCK_RE.sub("", content)
    value = _META_PREFIX_RE.sub("", value)
    cut = value.find("<memory-context>")
    if cut >= 0:
        value = value[:cut]
    return " ".join(value.split())


def _clip(value: str, limit: int) -> str:
    value = " ".join((value or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _tail(value: str, limit: int) -> str:
    value = " ".join((value or "").split())
    return value if len(value) <= limit else "…" + value[-(limit - 1):]


def _meaningful_tail(reply: str, n_lines: int = 2) -> str:
    lines = [line.strip() for line in (reply or "").strip().splitlines()]
    kept: List[str] = []
    for line in reversed(lines):
        if not line or _NOISE_LINE_RE.match(line):
            continue
        kept.append(_URL_RE.sub("", line))
        if len(kept) >= n_lines:
            break
    return "\n".join(reversed(kept))


def _asks_user(reply: str) -> bool:
    meaningful = [line for line in (reply or "").splitlines() if line.strip()]
    return bool(meaningful and _ASK_RE.search(meaningful[-1]))


def _iso_to_epoch(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.timestamp()
    except Exception:  # health: allow BLE001 -- Pinned upstream display timestamp parser returns no timestamp on bad input.
        return None


def _numeric_id(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text if _DIGITS_RE.fullmatch(text) else None


def _discord_links(
    source: Any,
    origin: Dict[str, Any],
    channel: Any,
    chat_type: Any,
    message_id: Any = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if source != "discord":
        return None, None, None
    channel_id = _numeric_id(channel)
    if not channel_id:
        return None, None, None
    if message_id not in (None, "") and not _numeric_id(message_id):
        return None, None, None
    guild_id = _numeric_id(origin.get("guild_id"))
    if guild_id:
        path = f"{guild_id}/{channel_id}"
    elif chat_type == "dm":
        path = f"@me/{channel_id}"
    else:
        return None, None, None
    valid_message = _numeric_id(message_id)
    if valid_message:
        path = f"{path}/{valid_message}"
    web = f"https://discord.com/channels/{path}"
    app = f"discord://-/channels/{path}"
    return web, app, web if valid_message else None


def _origin_for(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        value = json.loads(_row_get(row, "origin_json") or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:  # health: allow BLE001 -- Pinned upstream optional origin parser discards malformed metadata.
        return {}


def _session_meta(row: sqlite3.Row, people: Dict[str, str], message_id: Any = None) -> Dict[str, Any]:
    origin = _origin_for(row)
    source = _row_get(row, "source")
    display = _row_get(row, "display_name") or origin.get("chat_name") or ""
    where = display
    if source == "discord" and " / " in display:
        where = " / ".join(display.split(" / ")[1:])
    title = _row_get(row, "title") or (display.split(" / ")[-1] if display else _row_get(row, "id"))
    if str(title).startswith("["):
        title = display.split(" / ")[-1] if display else title
    if " / " in str(where):
        parts = str(where).split(" / ")
        if len(parts) > 1 and parts[-1].strip() == str(title).strip():
            where = " / ".join(parts[:-1])
    user_id = str(_row_get(row, "user_id") or origin.get("user_id") or "")
    who = people.get(user_id) or origin.get("user_name") or ""
    channel = _row_get(row, "thread_id") or _row_get(row, "chat_id") or origin.get("thread_id") or origin.get("chat_id")
    link, app_link, message_link = _discord_links(
        source, origin, channel, _row_get(row, "chat_type") or origin.get("chat_type"), message_id,
    )
    return {
        "session_id": _row_get(row, "id"),
        "source": source,
        "title": _clip(str(title or ""), 60),
        "where": _clip(str(where or ""), 60),
        "link": link,
        "app_link": app_link,
        "message_link": message_link,
        "who": str(who or ""),
    }


def _recent_messages(conn: sqlite3.Connection, sid: str, columns: Set[str]) -> List[sqlite3.Row]:
    selected = [column for column in (
        "id", "role", "content", "timestamp", "display_kind", "platform_message_id",
    ) if column in columns]
    # Hermes keeps undone/rewound rows with active=0; only the live transcript counts.
    live = "AND active=1" if "active" in columns else ""
    return conn.execute(
        f"""SELECT {', '.join(selected)} FROM messages
           WHERE session_id=? AND role IN ('user','assistant')
             AND content IS NOT NULL AND content != '' {live}
           ORDER BY timestamp DESC LIMIT ?""",
        (sid, SCAN_LIMIT_MSGS),
    ).fetchall()


_ORIGIN_JSON_RE = re.compile(r"^\s*Gateway message origin \(JSON data[^\n]*\n(\{[^\n]*\})", re.S)
_VERIFIED_SENDER_RE = re.compile(r"\[Verified sender:[^\]]*?\buser_id ([0-9A-Za-z._:@<>-]+)")


def _message_sender(content: str) -> Optional[str]:
    """Sender user id recorded by the gateway on a user row, when present."""
    match = _ORIGIN_JSON_RE.match(content or "")
    if match:
        try:
            value = json.loads(match.group(1)).get("user_id")
            if value not in (None, ""):
                return str(value)
        except (ValueError, AttributeError):
            pass
    match = _VERIFIED_SENDER_RE.search((content or "")[:600])
    if match:
        return match.group(1).strip("<@>")
    return None


# ── collectors ────────────────────────────────────────────────────────────

_SESSION_COLUMNS = (
    "id", "source", "user_id", "title", "display_name", "origin_json", "chat_id", "thread_id",
    "started_at", "ended_at", "last_activity_at", "last_activity_description",
)
_MESSAGE_COLUMNS = ("id", "session_id", "role", "content", "timestamp", "display_kind")
_LEASE_COLUMNS = ("conversation_id", "acquired_at", "expires_at")
_DELEGATION_COLUMNS = (
    "delegation_id", "state", "delivery_state", "dispatched_at", "completed_at", "updated_at",
    "parent_session_id", "task_json", "result_json",
)


def _collect_conversations(  # health: allow CC -- Keep the reviewed pinned collector intact; its state database is read-only.
    conn: sqlite3.Connection,
    now: float,
    schema: Optional[Dict[str, Set[str]]] = None,
    warnings: Optional[List[Dict[str, str]]] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    schema = schema or _schema_snapshot(conn)
    warnings = warnings if warnings is not None else []
    settings = _runtime_settings() if settings is None else settings
    people = settings["people"]
    dismissed = _dismissed()
    sessions_missing = _missing(schema, "sessions", _SESSION_COLUMNS)
    messages_missing = _missing(schema, "messages", _MESSAGE_COLUMNS)
    if sessions_missing or messages_missing:
        for section in ("waiting", "unanswered", "done", "running"):
            for missing in sessions_missing + messages_missing:
                _warn(warnings, section, missing)
        return {"running": [], "waiting": [], "unanswered": [], "done": []}

    lease_missing = _missing(schema, "session_turn_leases", _LEASE_COLUMNS)
    leases: Dict[str, sqlite3.Row] = {}
    if lease_missing:
        for missing in lease_missing:
            _warn(warnings, "running", missing)
    else:
        leases = {
            row["conversation_id"]: row for row in conn.execute(
                "SELECT conversation_id, acquired_at, expires_at FROM session_turn_leases WHERE expires_at > ?", (now,)
            )
        }

    placeholders = ",".join("?" for _ in settings["messaging_sources"])
    session_select = list(_SESSION_COLUMNS)
    if "chat_type" in schema["sessions"]:
        session_select.insert(6, "chat_type")
    rows = conn.execute(
        f"""SELECT {', '.join(session_select)} FROM sessions
            WHERE source IN ({placeholders}) AND ended_at IS NULL
              AND COALESCE(last_activity_at, started_at) > ?""",
        (*settings["messaging_sources"], now - settings["window_days"] * 86400),
    ).fetchall()

    running: List[Dict[str, Any]] = []
    waiting: List[Dict[str, Any]] = []
    unanswered: List[Dict[str, Any]] = []
    done: List[Dict[str, Any]] = []
    for row in rows:
        sid = row["id"]
        messages = _recent_messages(conn, sid, schema["messages"])
        last_real: Optional[sqlite3.Row] = None
        latest_user: Optional[Tuple[str, float]] = None
        latest_sender: Optional[str] = None
        target_message_id: Optional[str] = None
        last_any_ts = 0.0
        for message in messages:
            content = _text(_row_get(message, "content"))
            last_any_ts = max(last_any_ts, float(_row_get(message, "timestamp") or 0))
            role = _row_get(message, "role")
            if role == "assistant" and _row_get(message, "display_kind") == "hidden":
                continue
            if role == "user" and _is_internal(content, _row_get(message, "display_kind")):
                continue
            if last_real is None:
                last_real = message
            if role == "user":
                if target_message_id is None and _row_get(message, "platform_message_id") not in (None, ""):
                    target_message_id = str(_row_get(message, "platform_message_id"))
                if latest_user is None:
                    cleaned = _clean_user(content)
                    if cleaned:
                        latest_user = (cleaned, float(_row_get(message, "timestamp") or 0))
                        latest_sender = _message_sender(content)
            if last_real is not None and latest_user is not None:
                break
        activity_at = float(_row_get(row, "last_activity_at") or last_any_ts or _row_get(row, "started_at") or 0)
        meta = _session_meta(row, people, target_message_id)
        # Shared threads can hold several people. When the gateway recorded who sent the
        # latest message, that person owns the turn; otherwise fall back to the session owner.
        uid = latest_sender or str(_row_get(row, "user_id") or "")
        base = dict(meta)
        if latest_sender and latest_sender != str(_row_get(row, "user_id") or ""):
            base["who"] = people.get(latest_sender, "")
        base.update({
            "mine": _is_owner(uid, settings),
            "ask": _clip(latest_user[0], 140) if latest_user else "",
            "ask_at": latest_user[1] if latest_user else None,
            "activity_at": activity_at,
        })

        if sid in leases:
            started = latest_user[1] if latest_user else float(leases[sid]["acquired_at"] or now)
            quiet = now - activity_at if activity_at else None
            base.update({
                "started_at": started,
                "doing": _clip(_row_get(row, "last_activity_description") or "", 80),
                "quiet_s": quiet,
                "stalled": bool(quiet is not None and quiet > settings["stall_seconds"]),
            })
            running.append(base)
            continue
        if last_real is None:
            continue
        message_ts = float(_row_get(last_real, "timestamp") or 0)
        if _row_get(last_real, "role") == "user":
            if now - message_ts > settings["unanswered_grace_seconds"]:
                key = f"{sid}:{_row_get(last_real, 'id')}"
                if key not in dismissed:
                    base.update({"since": message_ts, "dismiss_key": key})
                    unanswered.append(base)
            continue
        reply = _text(_row_get(last_real, "content"))
        key = f"{sid}:{_row_get(last_real, 'id')}"
        tail = _meaningful_tail(reply)
        base.update({
            "since": message_ts,
            "reply_tail": _tail(_strip_md(tail or reply), 180),
            "dismiss_key": key,
        })
        if _asks_user(tail):
            if key not in dismissed:
                waiting.append(base)
        elif now - message_ts < settings["recent_done_hours"] * 3600:
            base["promised"] = bool(_PROMISE_RE.search(_meaningful_tail(reply, 3)))
            done.append(base)

    if settings["hide_other_people_content"] and settings["owner"]:
        waiting = [item for item in waiting if item["mine"]]
        unanswered = [item for item in unanswered if item["mine"]]
        done = [item for item in done if item["mine"]]
        for item in running:
            if not item["mine"]:
                item.update({
                    "title": "", "where": "", "ask": "", "doing": "", "link": None,
                    "app_link": None, "message_link": None, "private": True,
                })

    running.sort(key=lambda item: item.get("started_at") or 0)
    waiting.sort(key=lambda item: item["since"])
    unanswered.sort(key=lambda item: item["since"])
    done.sort(key=lambda item: item["since"], reverse=True)
    return {"running": running, "waiting": waiting, "unanswered": unanswered, "done": done[:15]}


def _session_title(conn: sqlite3.Connection, sid: Optional[str], people: Dict[str, str], schema: Dict[str, Set[str]]) -> Dict[str, Any]:
    if not sid or not schema.get("sessions"):
        return {}
    required = ("id", "source", "user_id", "title", "display_name", "origin_json", "chat_id", "thread_id")
    if _missing(schema, "sessions", required):
        return {}
    selected = list(required)
    if "chat_type" in schema["sessions"]:
        selected.insert(6, "chat_type")
    row = conn.execute(f"SELECT {', '.join(selected)} FROM sessions WHERE id=?", (sid,)).fetchone()
    return _session_meta(row, people) if row else {}


def _load_task(raw: Optional[str]) -> Dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {}
    except Exception:  # health: allow BLE001 -- Pinned upstream optional task parser discards malformed metadata.
        return {}


def _result_summary(raw: Optional[str]) -> str:
    try:
        value = json.loads(raw) if raw else {}
    except Exception:  # health: allow BLE001 -- Pinned upstream optional result parser discards malformed metadata.
        return ""
    if isinstance(value, dict):
        results = value.get("results") or []
        parts = [str(item.get("summary") or "") for item in results if isinstance(item, dict)]
        return " / ".join(part for part in parts if part) or str(value.get("summary") or "")
    return ""


def _collect_delegations(
    conn: sqlite3.Connection,
    now: float,
    schema: Optional[Dict[str, Set[str]]] = None,
    warnings: Optional[List[Dict[str, str]]] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    schema = schema or _schema_snapshot(conn)
    warnings = warnings if warnings is not None else []
    missing = _missing(schema, "async_delegations", _DELEGATION_COLUMNS)
    if missing:
        for item in missing:
            _warn(warnings, "delegations", item)
        return {"running": [], "dropped": []}
    settings = _runtime_settings() if settings is None else settings
    dismissed = _dismissed()
    running: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    origin_col = "origin_session_id" if "origin_session_id" in schema["async_delegations"] else "NULL AS origin_session_id"
    rows = conn.execute(
        f"""SELECT delegation_id, state, delivery_state, dispatched_at, completed_at, updated_at,
                  parent_session_id, {origin_col}, task_json, result_json
           FROM async_delegations
           WHERE state='running' OR (delivery_state='dropped' AND COALESCE(completed_at, updated_at) > ?)
           ORDER BY dispatched_at DESC""",
        (now - DROPPED_LOOKBACK_DAYS * 86400,),
    ).fetchall()
    for row in rows:
        task = _load_task(row["task_json"])
        goal = task.get("goal") or ""
        goals = task.get("goals") or []
        user_id = task.get("user_id")
        item: Dict[str, Any] = {
            "id": row["delegation_id"], "goal": _clip(str(goal), 140),
            "count": len(goals) if isinstance(goals, list) and goals else 1,
            "model": task.get("model") or "", "dispatched_at": row["dispatched_at"],
            "session": _session_title(conn, row["parent_session_id"] or row["origin_session_id"], settings["people"], schema),
        }
        private = not _is_owner(user_id, settings)
        if private:
            item.update({
                "private": True,
                "who": settings["people"].get(str(user_id), task.get("user_name") or ""),
                "goal": "", "model": "", "session": {},
            })
        if row["state"] == "running":
            running.append(item)
        elif row["delegation_id"] not in dismissed:
            item["completed_at"] = row["completed_at"] or row["updated_at"]
            item["summary"] = "" if private else _clip(_strip_md(_result_summary(row["result_json"])), 200)
            item["dismiss_key"] = row["delegation_id"]
            dropped.append(item)
    return {"running": running, "dropped": dropped}


_MD_RE = re.compile(r"\*\*|__|`+|^#+\s*|^[-*]\s+", re.M)


def _strip_md(text: str) -> str:
    return _MD_RE.sub("", text or "")


def _collect_cron(now: float) -> Dict[str, Any]:
    raw = _load_json(_home() / "cron" / "jobs.json", {})
    jobs = raw.get("jobs", []) if isinstance(raw, dict) else (raw or [])
    if not isinstance(jobs, list):
        jobs = []
    enabled = [job for job in jobs if isinstance(job, dict) and job.get("enabled")]
    problems: List[Dict[str, Any]] = []
    running: List[Dict[str, Any]] = []
    upcoming: List[Dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if not job.get("enabled") and job.get("state") != "paused":
            continue
        next_at = _iso_to_epoch(job.get("next_run_at"))
        last_at = _iso_to_epoch(job.get("last_run_at"))
        item: Dict[str, Any] = {
            "id": job.get("id"), "name": _clip(job.get("name") or job.get("id") or "", 48),
            "schedule": job.get("schedule_display") or (job.get("schedule") or {}).get("display") or "",
            "next_at": next_at, "last_at": last_at, "last_status": job.get("last_status"),
            "deliver": job.get("deliver") or "", "script_only": bool(job.get("no_agent")),
        }
        reasons: List[Dict[str, Any]] = []
        if job.get("fire_claim"):
            running.append(item)
        if job.get("last_status") == "error":
            errors = (job.get("last_error") or "").strip().splitlines()
            reasons.append({"code": "last_error", "detail": _clip(errors[-1] if errors else "", 90)})
        if job.get("last_delivery_error"):
            reasons.append({"code": "delivery_error", "detail": _clip(str(job.get("last_delivery_error")), 80)})
        if job.get("state") != "scheduled":
            reasons.append({"code": "state", "detail": str(job.get("state") or "")})
        if next_at and next_at < now - 600 and not job.get("fire_claim"):
            reasons.append({"code": "overdue"})
        if reasons:
            item["reasons"] = reasons
            problems.append(item)
        elif next_at and job.get("enabled"):
            upcoming.append(item)
    upcoming.sort(key=lambda item: item["next_at"] or 0)
    return {
        "enabled": len(enabled),
        "paused": sum(1 for job in jobs if not job.get("enabled") and job.get("state") == "paused"),
        "problems": problems, "running": running, "upcoming": upcoming[:8],
    }


def _collect_goals(
    conn: sqlite3.Connection,
    now: float,
    schema: Optional[Dict[str, Set[str]]] = None,
    warnings: Optional[List[Dict[str, str]]] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    schema = schema or _schema_snapshot(conn)
    warnings = warnings if warnings is not None else []
    state_missing = _missing(schema, "state_meta", ("key", "value"))
    session_missing = _missing(schema, "sessions", ("id", "source", "user_id", "title", "display_name", "origin_json", "chat_id", "thread_id", "started_at", "last_activity_at"))
    if state_missing or session_missing:
        for missing in state_missing + session_missing:
            _warn(warnings, "goals", missing)
        return {"items": [], "stale": 0}
    settings = _runtime_settings() if settings is None else settings
    live: List[Dict[str, Any]] = []
    stale = 0
    for meta in conn.execute("SELECT key, value FROM state_meta WHERE key LIKE 'goal:%'").fetchall():
        try:
            goal = json.loads(meta["value"])
        except Exception:  # health: allow BLE001 -- Pinned upstream goal collector skips malformed JSON entries.
            continue
        if not isinstance(goal, dict) or goal.get("status") not in ("active", "paused"):
            continue
        sid = meta["key"].split(":", 1)[1]
        selected = ["id", "source", "user_id", "title", "display_name", "origin_json", "chat_id", "thread_id", "started_at", "last_activity_at"]
        if "chat_type" in schema["sessions"]:
            selected.insert(6, "chat_type")
        row = conn.execute(
            f"SELECT {', '.join(selected)}, COALESCE(last_activity_at, started_at) AS act FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        if row is None or row["source"] not in settings["messaging_sources"]:
            continue
        if (row["act"] or 0) < now - settings["goal_lookback_days"] * 86400:
            stale += 1
            continue
        if not _is_owner(row["user_id"], settings):
            continue
        live.append({
            "goal": _clip(goal.get("goal") or "", 120), "status": goal.get("status"),
            "turns": goal.get("turns_used"), "max_turns": goal.get("max_turns"),
            "reason": _clip(goal.get("last_reason") or goal.get("paused_reason") or "", 120),
            "session": _session_meta(row, settings["people"]), "activity_at": row["act"],
        })
    live.sort(key=lambda item: item["activity_at"] or 0, reverse=True)
    return {"items": live, "stale": stale}


def _collect_gateway() -> Dict[str, Any]:
    state = _load_json(_home() / "gateway_state.json", {})
    if not isinstance(state, dict):
        state = {}
    platforms = []
    for name, platform in (state.get("platforms") or {}).items():
        if isinstance(platform, dict):
            platforms.append({"name": name, "state": platform.get("state"), "error": platform.get("error_message")})
    return {"state": state.get("gateway_state"), "version": state.get("code_version"), "platforms": platforms}


def _run_collector(name: str, fn: Callable[[], Any], default: Any, warnings: List[Dict[str, str]]) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - one broken section must not take down the board
        logger.exception("Duty Board %s collector failed", name)
        _warn(warnings, name, {"code": "collectorFailed", "detail": type(exc).__name__})
        return default


def build(now: float, *, settings=None) -> Dict[str, Any]:
    settings = _runtime_settings() if settings is None else settings
    warnings = _Issues()
    empty_conversations = {"running": [], "waiting": [], "unanswered": [], "done": []}
    empty_delegations = {"running": [], "dropped": []}
    conn: Optional[sqlite3.Connection] = None
    schema: Dict[str, Set[str]] = {}
    try:
        conn = _connect()
        schema = _schema_snapshot(conn)
        conversations = _run_collector(
            "conversations", lambda: _collect_conversations(conn, now, schema, warnings, settings), empty_conversations, warnings,
        )
        delegations = _run_collector(
            "delegations", lambda: _collect_delegations(conn, now, schema, warnings, settings), empty_delegations, warnings,
        )
        goals = _run_collector(
            "goals", lambda: _collect_goals(conn, now, schema, warnings, settings), {"items": [], "stale": 0}, warnings,
        )
    except Exception as exc:  # noqa: BLE001 - a missing/corrupt DB still yields a useful page
        logger.exception("Duty Board state database collector failed")
        _warn(warnings, "database", {"code": "collectorFailed", "detail": type(exc).__name__})
        conversations, delegations, goals = empty_conversations, empty_delegations, {"items": [], "stale": 0}
    finally:
        if conn is not None:
            conn.close()
    cron = _run_collector("cron", lambda: _collect_cron(now), {"enabled": 0, "paused": 0, "problems": [], "running": [], "upcoming": []}, warnings)
    gateway = _run_collector("gateway", _collect_gateway, {"state": None, "version": None, "platforms": []}, warnings)
    return {
        "schema_version": 2,
        "plugin_version": __version__,
        "generated_at": now,
        "issues": list(warnings),
        "warnings": warnings.as_text(),
        "gateway": gateway,
        "conversations": conversations,
        "delegations": delegations,
        "cron": cron,
        "goals": goals,
    }


def cached_summary(*, settings=None) -> Dict[str, Any]:
    now = time.time()
    settings = _runtime_settings() if settings is None else settings
    home = (str(_home()), json.dumps(settings, sort_keys=True))
    with _cache_lock:
        if _cache["data"] is not None and _cache["home"] == home and now - _cache["at"] < CACHE_TTL_S:
            return _cache["data"]
    data = build(now, settings=settings)
    with _cache_lock:
        _cache.update(at=now, data=data, home=home)
    return data
