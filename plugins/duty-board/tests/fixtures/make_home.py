"""Synthetic Hermes home builder used by the API tests and the dev harness."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional


SESSION_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT, session_key TEXT,
    chat_id TEXT, chat_type TEXT, thread_id TEXT, display_name TEXT, origin_json TEXT,
    started_at REAL NOT NULL, ended_at REAL, title TEXT, last_activity_at REAL,
    last_activity_description TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT, timestamp REAL NOT NULL, display_kind TEXT, platform_message_id TEXT,
    active INTEGER NOT NULL DEFAULT 1, compacted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE session_turn_leases (
    conversation_id TEXT PRIMARY KEY, holder TEXT NOT NULL, acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE async_delegations (
    delegation_id TEXT PRIMARY KEY, origin_session TEXT NOT NULL DEFAULT '',
    origin_ui_session_id TEXT NOT NULL DEFAULT '', parent_session_id TEXT,
    state TEXT NOT NULL, dispatched_at REAL NOT NULL, completed_at REAL,
    updated_at REAL NOT NULL, event_json TEXT, result_json TEXT,
    delivery_state TEXT NOT NULL DEFAULT 'pending', delivery_attempts INTEGER NOT NULL DEFAULT 0,
    delivered_at REAL, owner_pid INTEGER, owner_started_at INTEGER, task_json TEXT,
    delivery_claim TEXT, delivery_claimed_at REAL, origin_session_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
"""


def make_home(root: Path, *, now: Optional[float] = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    stamp = float(now if now is not None else time.time())
    conn = sqlite3.connect(root / "state.db")
    conn.executescript(SESSION_DDL)
    conn.commit()
    conn.close()
    (root / "cron").mkdir()
    (root / "cron" / "jobs.json").write_text(json.dumps({"jobs": []}), encoding="utf-8")
    (root / "gateway_state.json").write_text(json.dumps({"gateway_state": "connected", "platforms": {}}), encoding="utf-8")
    (root / "duty-board").mkdir()
    (root / "duty-board" / "settings.json").write_text("{}", encoding="utf-8")
    (root / "fixture-now").write_text(str(stamp), encoding="utf-8")
    return root


def db(home: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(home / "state.db")
    conn.row_factory = sqlite3.Row
    return conn


def add_session(
    home: Path,
    session_id: str,
    *,
    source: str = "discord",
    user_id: str = "222222222222222222",
    chat_id: str = "333333333333333333",
    thread_id: Optional[str] = None,
    chat_type: str = "group",
    guild_id: Optional[str] = "111111111111111111",
    user_name: str = "Alice",
    title: str = "Alice's thread",
    now: Optional[float] = None,
) -> None:
    stamp = float(now if now is not None else float((home / "fixture-now").read_text(encoding="utf-8-sig")))
    origin: Dict[str, Any] = {"user_id": user_id, "user_name": user_name, "chat_id": chat_id}
    if guild_id is not None:
        origin["guild_id"] = guild_id
    conn = db(home)
    conn.execute(
        """INSERT INTO sessions
        (id, source, user_id, session_key, chat_id, chat_type, thread_id, display_name,
         origin_json, started_at, ended_at, title, last_activity_at, last_activity_description)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
        (session_id, source, user_id, session_id, chat_id, chat_type, thread_id, f"Server / {title}",
         json.dumps(origin), stamp - 3600, title, stamp, "starting new turn"),
    )
    conn.commit()
    conn.close()


def add_message(
    home: Path,
    session_id: str,
    role: str,
    content: str,
    *,
    timestamp: Optional[float] = None,
    platform_message_id: Optional[str] = None,
    display_kind: Optional[str] = None,
) -> int:
    stamp = timestamp if timestamp is not None else float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    conn = db(home)
    cursor = conn.execute(
        """INSERT INTO messages
        (session_id, role, content, timestamp, display_kind, platform_message_id)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, role, content, stamp, display_kind, platform_message_id),
    )
    conn.commit()
    message_id = int(cursor.lastrowid or 0)
    conn.close()
    return message_id


def add_lease(home: Path, session_id: str, *, now: Optional[float] = None, ttl: float = 600) -> None:
    stamp = float(now if now is not None else float((home / "fixture-now").read_text(encoding="utf-8-sig")))
    conn = db(home)
    conn.execute(
        "INSERT INTO session_turn_leases (conversation_id, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
        (session_id, "fixture-holder", stamp - 30, stamp + ttl),
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    make_home(args.path)
