"""Threads, read side: the store queries that the ``thread`` tool and the parent wake share.

A thread is one of the user's local chats that a ``thread`` tool call opened. ``session.create``
records its launch provenance in the CHILD profile's store: a ``work_launches`` row whose
``launch_id`` starts with ``thread:``, bound to the launching chat (``launcher_profile`` +
``launcher_session_id``). Family stitches those rows across profiles; this module reads the
same rows, plus the canonical admission ledger, through plain read-only connections, so it holds
no SessionDB lock and never writes a store.

Tool-sent turns carry a ``thread-`` request id, which is how a waiter or the wake tells
Ava's work in a thread from the user's own turns there.

Delivery markers are the one piece of state this feature owns, under the launching profile's
``cache/threads/delivered``, one per (admission, transition): a turn that paused is reported paused
once, and again once it settles. ``wait`` and ``read`` mark a result delivered when they hand it to
the parent. The wake claims first (``<key>.claim``, ``O_EXCL``), submits, and only then marks the
result delivered; a refused submit releases the claim, and a claim left by a crash goes stale and is
retaken, so a result reaches its parent at least once and normally exactly once.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from pathlib import Path

LAUNCH_PREFIX = "thread:"
REQUEST_PREFIX = "thread-"
MARK = "\u21b3"  # ↳
#: Admissions the wake still looks at: a thread active within this window. Shorter than the marker
#: lifetime, so a pruned marker can never make an old result fire again.
WAKE_WINDOW_S = 6 * 24 * 3600
MARKER_TTL_S = 7 * 24 * 3600
#: A claim older than this was left by a wake that died between claiming and submitting.
CLAIM_STALE_S = 120
#: Admission outcome -> thread status.
SETTLED = {"completed": "done", "failed": "failed", "interrupted": "stopped", "cancelled": "stopped"}


def homes() -> dict[str, Path]:
    """Every live profile's home, by profile name (``default`` included)."""
    from hermes_cli.profiles import get_profile_dir, list_profile_names
    out = {}
    for name in list_profile_names():
        with contextlib.suppress(FileNotFoundError, ValueError):  # a profile removed or renamed mid-read
            out[name] = get_profile_dir(name).resolve()
    if "default" not in out:
        from hermes_constants import get_default_hermes_root
        out["default"] = get_default_hermes_root().resolve()
    return out


def profile_of(home: Path) -> str:
    home = Path(home).resolve()
    return next((name for name, path in homes().items() if path == home), "default")


@contextlib.contextmanager
def reader(home: Path):
    """A read-only connection to one profile's store, or None when it has no store yet."""
    path = Path(home) / "state.db"
    if not path.exists():
        yield None
        return
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=1")
        yield conn
    finally:
        conn.close()


def _marks(ids) -> str:
    return ",".join("?" * len(ids))


def lineage(conn, session_id: str) -> frozenset:
    """The session plus its compression continuations, in both directions (one conversation)."""
    from hermes_state_sessions import _expand_compression_lineage_ids
    seen, frontier = {session_id}, [session_id]
    for _ in range(32):
        frontier = [sid for sid in _expand_compression_lineage_ids(conn, frontier) if sid not in seen]
        if not frontier:
            break
        seen.update(frontier)
    return frozenset(seen)


def tip(conn, session_id: str) -> str:
    """The live end of a compression chain, walked forward with the store's own chain step
    (``SessionDB.get_compression_tip`` semantics) on this connection."""
    from hermes_state_compression import _CHAIN_STEP_SQL
    current, seen = session_id, {session_id}
    for _ in range(64):
        row = conn.execute(_CHAIN_STEP_SQL, (current,)).fetchone()
        if row is None or not row[0] or row[0] in seen:
            return current
        current = row[0]
        seen.add(current)
    return current


def launches(conn, launcher_profile: str, launcher_ids) -> list[dict]:
    """Thread launches one profile's store records for chats launched by these sessions."""
    ids = list(launcher_ids)
    return [dict(r) for r in conn.execute(
        "SELECT launch_id, kind, child_profile, child_session_id, launcher_session_id, title, created_at "
        f"FROM work_launches WHERE substr(launch_id,1,{len(LAUNCH_PREFIX)})=? AND child_session_id IS NOT NULL "
        f"AND launcher_profile=? AND launcher_session_id IN ({_marks(ids)}) ORDER BY created_at",
        [LAUNCH_PREFIX, launcher_profile, *ids])]


def threads_of(launcher_profile: str, launcher_ids) -> list[dict]:
    """Every thread these sessions opened, across all profiles (each store holds its own children)."""
    found = []
    for profile, home in homes().items():
        with reader(home) as conn:
            if conn is not None:
                found += [row for row in launches(conn, launcher_profile, launcher_ids)
                          if row["child_profile"] == profile]
    return sorted(found, key=lambda row: row["created_at"] or 0)


def launch_of(conn, profile: str, ids) -> dict | None:
    """The thread launch that opened this chat, if it is a thread."""
    ids = list(ids)
    row = conn.execute(
        "SELECT launch_id, kind, launcher_profile, launcher_session_id, child_session_id, created_at FROM work_launches "
        f"WHERE substr(launch_id,1,{len(LAUNCH_PREFIX)})=? AND child_profile=? AND child_session_id IN ({_marks(ids)}) "
        "LIMIT 1", [LAUNCH_PREFIX, profile, *ids]).fetchone()
    return dict(row) if row else None


def admissions(conn, ids) -> list[dict]:
    ids = list(ids)
    return [dict(r) for r in conn.execute(
        "SELECT seq, admission_id, request_id, status, outcome, generation, target_session_id FROM session_admissions "
        f"WHERE target_session_id IN ({_marks(ids)}) ORDER BY seq", ids)]


def tool_sent(admission: dict) -> bool:
    return str(admission.get("request_id") or "").startswith(REQUEST_PREFIX)


def admission_status(admission: dict | None) -> str:
    if admission is None:
        return "idle"
    live = {"queued": "queued", "started": "running", "unknown": "paused"}.get(admission["status"])
    return live or SETTLED.get(str(admission.get("outcome") or ""), "failed")


def thread_status(rows: list[dict]) -> str:
    """One status for the whole thread: what it is doing now, else how its newest turn ended."""
    states = {row["status"] for row in rows}
    if "started" in states:
        return "running"
    if "unknown" in states:
        return "paused"  # a restart cut a turn off; the chat's queue waits until it is resolved
    if "queued" in states:
        return "queued"
    return admission_status(rows[-1]) if rows else "idle"


def pinned(rows: list[dict], admission_id: str | None = None) -> dict | None:
    """The admission a waiter means: the named one, else the newest the tool sent, else the newest."""
    if admission_id:
        return next((row for row in rows if row["admission_id"] == admission_id), None)
    return next((row for row in reversed(rows) if tool_sent(row)), rows[-1] if rows else None)


def last_reply(conn, ids) -> str:
    ids = list(ids)
    row = conn.execute(
        f"SELECT content FROM messages WHERE session_id IN ({_marks(ids)}) AND role='assistant' "
        "AND content IS NOT NULL AND content<>'' ORDER BY id DESC LIMIT 1", ids).fetchone()
    return row[0] if row else ""


def outcome(conn, admission: dict, ids) -> dict:
    """What a settled admission produced: its final reply, error and usage, from the committed result."""
    from hermes_state_terminal import RESULT_PREFIX, load_result
    saved = conn.execute("SELECT value FROM state_meta WHERE key=?",
                         (RESULT_PREFIX + admission["admission_id"],)).fetchone()
    saved = load_result(saved[0]) if saved else None
    result = saved["result"] if isinstance(saved, dict) and isinstance(saved.get("result"), dict) else {}
    out = {"final_response": result.get("final_response") or "", "error": result.get("error")
           or result.get("failure_reason")}
    usage = {k: result[k] for k in ("api_calls", "estimated_cost_usd", "model") if result.get(k) is not None}
    if usage:
        out["usage"] = usage
    if not out["final_response"] and admission_status(admission) == "done":
        out["final_response"] = last_reply(conn, ids)
    return out


# ── delivery markers ─────────────────────────────────────────────────────────────────────────

def _marker_dir(home: Path) -> Path:
    return Path(home) / "cache" / "threads" / "delivered"


def _marker(home: Path, admission_id: str, status: str) -> Path:
    # A paused turn is reported once as paused, and again once it finally settles.
    return _marker_dir(home) / (admission_id + (".paused" if status == "paused" else ""))


def delivered(home: Path, admission_id: str, status: str) -> bool:
    return _marker(home, admission_id, status).exists()


def _create(path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        return True
    except FileExistsError:
        return False


def mark_delivered(home: Path, admission_id: str, status: str) -> None:
    """The parent has this result (a ``wait`` or ``read`` handed it over, or the wake's turn was admitted)."""
    _create(_marker(home, admission_id, status))


def claim(home: Path, admission_id: str, status: str, now: float | None = None) -> bool:
    """Take the wake's delivery of one result: False when it was delivered or another wake holds a
    live claim. A claim older than ``CLAIM_STALE_S`` is retaken."""
    if delivered(home, admission_id, status):
        return False
    path = _marker(home, admission_id, status).with_name(_marker(home, admission_id, status).name + ".claim")
    if _create(path):
        return True
    with contextlib.suppress(OSError):
        if (time.time() if now is None else now) - path.stat().st_mtime > CLAIM_STALE_S:
            path.unlink()
            return _create(path)
    return False


def finish_claim(home: Path, admission_id: str, status: str, accepted: bool) -> None:
    """After the submit: delivered when it was admitted; otherwise free for the next poll."""
    if accepted:
        mark_delivered(home, admission_id, status)
    with contextlib.suppress(FileNotFoundError):
        claim_path = _marker(home, admission_id, status)
        claim_path.with_name(claim_path.name + ".claim").unlink()


def prune_markers(home: Path, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with contextlib.suppress(FileNotFoundError):
        for path in _marker_dir(home).iterdir():
            with contextlib.suppress(OSError):
                if now - path.stat().st_mtime > MARKER_TTL_S:
                    path.unlink()


def settled_unreported(launcher_home: Path, launcher_profile: str, launcher_ids, now: float | None = None) -> list[dict]:
    """Tool-sent turns in this chat's threads that settled (or paused) and nobody handed back yet."""
    return wake_state(launcher_home, launcher_profile, launcher_ids, now)[0]


def wake_state(launcher_home: Path, launcher_profile: str, launcher_ids, now: float | None = None):
    """The wake's whole read: (results to hand back, how many tool-sent turns are still queued or running)."""
    now = time.time() if now is None else now
    out, working = [], 0
    by_profile = {}
    for row in threads_of(launcher_profile, launcher_ids):
        by_profile.setdefault(row["child_profile"], []).append(row)
    for profile, rows in by_profile.items():
        with reader(homes().get(profile, launcher_home)) as conn:
            if conn is None:
                continue
            for launch in rows:
                ids = lineage(conn, launch["child_session_id"])
                info = conn.execute(f"SELECT MAX(COALESCE(last_activity_at, started_at)) FROM sessions "
                                    f"WHERE id IN ({_marks(list(ids))})", list(ids)).fetchone()
                if not info or not info[0] or now - info[0] > WAKE_WINDOW_S:
                    continue
                history = admissions(conn, ids)
                current = tip(conn, launch["child_session_id"])
                title = conn.execute("SELECT title FROM sessions WHERE id=?", (current,)).fetchone()
                for admission in history:
                    status = admission_status(admission)
                    if not tool_sent(admission):
                        continue
                    if status in {"queued", "running"}:
                        working += 1
                        continue
                    if delivered(launcher_home, admission["admission_id"], status):
                        continue
                    item = {"profile": profile, "thread_id": current, "title": title[0] if title else None,
                            "admission_id": admission["admission_id"], "status": status,
                            "thread_status": thread_status(history)}
                    if status != "paused":
                        item.update(outcome(conn, admission, ids))
                    out.append(item)
    return out, working
