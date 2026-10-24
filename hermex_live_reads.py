"""Read-only Hermex catalog snapshots and exact, revision-bound history pages."""
from __future__ import annotations

import base64
from collections import OrderedDict
from datetime import UTC, datetime
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
import sqlite3
from contextlib import contextmanager

from hermes_state_messages import DISPLAY_VISIBLE_SQL
from hermes_state_compression import SessionCompressionMixin

_CURSOR_KEY = secrets.token_bytes(32)
_CATALOGS = OrderedDict()
_CATALOG_LOCK = threading.Lock()
_DISCOVERY_STAMPS = OrderedDict()
_FILE_HASHES = OrderedDict()


class LiveConflict(ValueError):
    """The caller's snapshot no longer names the current state."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cursor_encode(scope, position):
    data = json.dumps(position, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.digest(_CURSOR_KEY, digest(scope).encode() + data, "sha256")
    return base64.urlsafe_b64encode(signature + data).decode()


def cursor_decode(token, scope):
    try:
        raw = base64.b64decode(token, altchars=b"-_", validate=True)
        signature, data = raw[:32], raw[32:]
        if not hmac.compare_digest(signature, hmac.digest(_CURSOR_KEY, digest(scope).encode() + data, "sha256")):
            raise ValueError("signature")
        return json.loads(data)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise LiveConflict("cursor expired or scope changed") from exc


def refresh_skill_commands(sc):
    from tools.skills_tool import skill_search_dirs, _get_disabled_skill_names
    from tools.skill_ledger import TRANSIENT_DIRS
    roots, _ = skill_search_dirs()
    records = []
    for _priority, root in roots:
        for directory, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if name not in TRANSIENT_DIRS and not name.startswith(".")]
            for path in [Path(directory), *([Path(directory) / "SKILL.md"] if "SKILL.md" in files else [])]:
                stat = path.stat()
                records.append((str(path), stat.st_mtime_ns, stat.st_size))
    key = (sc._resolve_skill_commands_platform(), sc._resolve_skill_commands_home(), sc._resolve_skill_commands_project())
    stamp = digest([sorted(records), sorted(_get_disabled_skill_names())])
    with _CATALOG_LOCK:
        changed = _DISCOVERY_STAMPS.get(key) != stamp
    if changed:
        sc.scan_skill_commands()
        with _CATALOG_LOCK:
            _DISCOVERY_STAMPS[key] = stamp
            _DISCOVERY_STAMPS.move_to_end(key)
            while len(_DISCOVERY_STAMPS) > 128:
                _DISCOVERY_STAMPS.popitem(last=False)


def _file_hash(path, stat):
    key = (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    with _CATALOG_LOCK:
        cached = _FILE_HASHES.get(key)
    if cached is not None:
        return cached
    value = hashlib.sha256(path.read_bytes()).hexdigest()
    with _CATALOG_LOCK:
        _FILE_HASHES[key] = value
        while len(_FILE_HASHES) > 16384:
            _FILE_HASHES.popitem(last=False)
    return value


class _SnapshotLineage(SessionCompressionMixin):
    """Run the canonical compression walker on one already-borrowed read connection."""

    def __init__(self, db, conn):
        self.db, self.conn = db, conn

    def get_session(self, session_id):
        row = self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row is not None else None

    @contextmanager
    def _read_ctx(self):
        yield self.conn

    def _is_explicit_fork_child_row(self, row, *, include_reset=False):
        return self.db._is_explicit_fork_child_row(row, include_reset=include_reset)


def owned_lineage(db, session_id, profile, *, conn=None):
    from hermes_state_sessions import INTERNAL_LISTING_SOURCES
    chain = _SnapshotLineage(db, conn).get_compression_chain(session_id) if conn else db.get_compression_chain(session_id)
    if session_id not in chain:
        raise ValueError("session unavailable")
    for sid in chain:
        row = dict(conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone() or {}) if conn else db.get_session(sid)
        if (not row or row.get("profile_name") not in (None, profile)
                or str(row.get("source") or "").strip().lower() in INTERNAL_LISTING_SOURCES):
            raise ValueError("session unavailable")
    return chain


def _skill_signature(info, ledger_updates):
    from tools.skill_ledger import TRANSIENT_DIRS
    path = info.get("skill_md_path")
    if not path:
        return None, None
    root = Path(path).parent
    records, newest = [], 0
    for file in sorted(root.rglob("*")):
        if any(part in TRANSIENT_DIRS for part in file.relative_to(root).parts):
            continue
        if file.is_file():
            stat = file.stat()
            records.append([file.relative_to(root).as_posix(), _file_hash(file, stat),
                            stat.st_mtime_ns])
            newest = max(newest, stat.st_mtime_ns, ledger_updates.get(str(file), 0))
    if not records:
        return None, None
    return digest(records), datetime.fromtimestamp(newest / 1e9, UTC).isoformat()


def catalog_metadata(cat, skills, *, profile, scope, since_revision, successful):
    from tools.skill_ledger import list_entries
    ledger_updates = {}
    for entry in list_entries():
        try:
            timestamp = int(datetime.fromisoformat(entry["ts"]).timestamp() * 1e9)
        except (ValueError, KeyError, TypeError):
            continue
        for item in entry.get("after", []):
            ledger_updates.setdefault(item.get("path"), timestamp)
    signatures, entries = {}, []
    for slash, description in dict(cat.pairs).items():
        kind = "skill" if slash in skills else "command"
        signature, updated = _skill_signature(cat.skill_sources.get(slash, {}), ledger_updates) if kind == "skill" else (None, None)
        identity = f"{kind}:{slash}"
        signatures[identity] = signature
        entries.append({"id": identity, "slash": slash, "kind": kind, "description": description,
                        "icon": None, "is_new": None, "updated_at": updated, "mobile_action": None})
    # Include descriptions and command availability, not badges or observation time.
    revision = digest([scope, signatures, cat.pairs])
    with _CATALOG_LOCK:
        history = _CATALOGS.get(scope, OrderedDict())
        previous = history.get(since_revision) if since_revision is not None else next(reversed(history.values()), None)
        if successful and previous is not None:
            for entry in entries:
                signature = signatures[entry["id"]]
                if signature is not None:
                    entry["is_new"] = previous.get(entry["id"]) != signature
        if successful:
            history[revision] = signatures
            history.move_to_end(revision)
            while len(history) > 16:
                history.popitem(last=False)
            _CATALOGS[scope] = history
            _CATALOGS.move_to_end(scope)
            while len(_CATALOGS) > 128:
                _CATALOGS.popitem(last=False)
    return {"catalog_version": 1, "profile": profile, "revision": revision if successful else "",
            "observed_at": datetime.now(UTC).isoformat(), "entries": entries}


def lineage_rows_sql(db, conn, chain):
    """Native SQLite identities for legacy rows, durable display order when indexed."""
    slots = ','.join('?' for _ in chain)
    indexed = conn.execute(
        f"SELECT 1 FROM messages WHERE session_id IN ({slots}) AND (active=1 OR compacted=1) "
        "AND (display_identity IS NULL OR display_order IS NULL) LIMIT 1", chain).fetchone() is None
    if indexed:
        return f"""WITH display_rows AS (
            SELECT COALESCE(MAX(CASE WHEN active=1 THEN id END), MAX(id)) AS row_id,
                   MIN(display_order) AS logical_id FROM messages
            WHERE session_id IN ({slots}) AND (active=1 OR compacted=1){DISPLAY_VISIBLE_SQL}
            GROUP BY display_identity)"""
    _register_lineage_functions(db, conn)
    return f"""WITH normalized AS (
        SELECT id, active, role, content, timestamp, tool_call_id, tool_calls, tool_name,
               display_kind, display_metadata,
               CASE WHEN role='assistant' AND tool_calls IS NOT NULL
                    THEN hermex_stable_calls(tool_calls) END AS stable_calls
        FROM messages WHERE session_id IN ({slots}) AND (active=1 OR compacted=1){DISPLAY_VISIBLE_SQL}
    ), identities AS (
        SELECT id, active, MIN(id) OVER identity AS logical_id,
               ROW_NUMBER() OVER (identity ORDER BY active DESC, id DESC) AS preference
        FROM normalized
        WINDOW identity AS (PARTITION BY role,
            CASE WHEN stable_calls IS NOT NULL THEN NULL
                 WHEN role='user' THEN hermex_user_content(content, display_kind, display_metadata) ELSE content END,
            timestamp, tool_call_id, COALESCE(stable_calls, tool_calls), tool_name)
    ), display_rows AS (SELECT id AS row_id, logical_id FROM identities WHERE preference=1)"""


def _register_lineage_functions(db, conn):
    from hermes_state_identity import _stable_tool_key

    def stable_calls(calls):
        key = _stable_tool_key({"role": "assistant", "tool_calls": calls,
                                "timestamp": None, "tool_call_id": None, "tool_name": None})
        return json.dumps(key[4]) if key is not None else None

    def user_content(content, kind, metadata):
        return db._display_dedupe_key({"role": "user", "content": content, "display_kind": kind,
            "display_metadata": metadata, "timestamp": None, "tool_call_id": None,
            "tool_calls": None, "tool_name": None})[1]

    conn.create_function("hermex_stable_calls", 1, stable_calls, deterministic=True)
    conn.create_function("hermex_user_content", 3, user_content, deterministic=True)


def lineage_generation(conn, chain):
    slots = ','.join('?' for _ in chain)
    rows = conn.execute(
        f"SELECT session_id, MAX(id), SUM(active=0 AND compacted=0) FROM messages "
        f"WHERE session_id IN ({slots}) GROUP BY session_id", chain).fetchall()
    ends = conn.execute(f"SELECT id,end_reason FROM sessions WHERE id IN ({slots}) ORDER BY id", chain).fetchall()
    return digest([chain, [list(r) for r in rows], [list(r) for r in ends]])


def _lineage_rows(db, conn, chain):
    return conn.execute(lineage_rows_sql(db, conn, chain)
                        + "SELECT row_id, logical_id FROM display_rows ORDER BY logical_id", chain).fetchall()


@dataclass(frozen=True)
class _LineageIndex:
    logical_ids: dict[int, str]
    generation: str


def _display_messages(db, rows, home):
    from hermes_cli.web_routers.sessions import _project_for_display
    messages = [db._row_to_message_dict(row, warn_context="Hermex window", summary_flag=True) for row in rows]
    return _project_for_display(messages, home=home, inline_images=False)


def _snippet(message, query):
    from hermes_state_search import _flatten_text
    text = _flatten_text(message.get("display_content", message.get("content")))
    terms = [token.strip('"').rstrip("*") for token in re.findall(r'"[^"]*"|\S+', query)
             if token.upper() not in {"AND", "OR", "NOT", "NEAR"}]
    terms = [term for term in terms if term]
    pattern = re.compile("|".join(re.escape(term) for term in terms), re.IGNORECASE) if terms else None
    first = pattern.search(text) if pattern else None
    start = max(0, first.start() - 120) if first else 0
    snippet = text[start:start + 400]
    ranges = [{"start": len(snippet[:m.start()].encode("utf-16-le")) // 2,
               "length": len(m.group().encode("utf-16-le")) // 2} for m in pattern.finditer(snippet)] if pattern else []
    return snippet, ranges


def _search_batch(db, conn, home, hits, profile, cache, representatives, query):
    batches = {}
    for hit in hits:
        sid = hit["session_id"]
        if sid not in cache:
            try:
                chain = owned_lineage(db, sid, profile, conn=conn)
                for member in chain:
                    cache[member] = chain
            except ValueError:
                cache[sid] = None
        if cache[sid] is not None:
            batches.setdefault(tuple(cache[sid]), []).append(hit)
    selected = {}
    for chain, candidates in batches.items():
        if chain not in representatives:
            representatives[chain] = _LineageIndex(
                {r["row_id"]: str(r["logical_id"]) for r in _lineage_rows(db, conn, chain)},
                lineage_generation(conn, chain))
        logical_ids = representatives[chain].logical_ids
        ids = [hit["id"] for hit in candidates if hit["id"] in logical_ids]
        if not ids:
            continue
        rows = conn.execute("SELECT * FROM messages WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids).fetchall()
        for message in _display_messages(db, rows, home):
            if message.get("display_kind") == "hidden":
                continue
            snippet, ranges = _snippet(message, query)
            if "display_content" in message and not ranges:
                continue
            selected[message["id"]] = (chain, {
                "message_id": str(message["id"]), "logical_id": logical_ids[message["id"]],
                "source_session_id": message["session_id"], "role": message["role"],
                "snippet": snippet, "ranges": ranges})
    return selected


def _metadata_search(db, conn, profile, query, offset, limit):
    pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = conn.execute("SELECT id FROM sessions WHERE (id LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\') "
                        "ORDER BY id LIMIT ? OFFSET ?", (pattern, pattern, limit + 1, offset)).fetchall()
    groups = {}
    for row in rows[:limit]:
        try:
            chain = owned_lineage(db, row["id"], profile, conn=conn)
        except ValueError:
            continue
        groups[chain[0]] = {"session_id": chain[-1], "lineage_root": chain[0], "matches": [], "match_count": None}
    return list(groups.values()), offset + limit if len(rows) > limit else None


_SEARCHES = {}
_SEARCH_LOCK = threading.Lock()
_REVISIONS = OrderedDict()
_SEARCH_BUDGET = 1.5


@contextmanager
def _search_snapshot(db, principal, home):
    from hermes_state_timeline import _snapshot
    cancelled = threading.Event()
    key = (principal, str(home))
    with _SEARCH_LOCK:
        previous = _SEARCHES.get(key)
        if previous is not None:
            previous.set()
        _SEARCHES[key] = cancelled
    deadline = time.monotonic() + _SEARCH_BUDGET
    try:
        with _snapshot(db) as conn:
            conn.set_progress_handler(lambda: cancelled.is_set() or time.monotonic() >= deadline, 1000)
            try:
                yield conn, deadline, cancelled
            finally:
                conn.set_progress_handler(None, 0)
    finally:
        with _SEARCH_LOCK:
            if _SEARCHES.get(key) is cancelled:
                del _SEARCHES[key]


def _revision_scope(principal, profile, home):
    return ["lineage-revision", principal, profile, str(home), _store_identity(home)]


def _store_identity(home):
    stat = (home / "state.db").stat()
    return [stat.st_dev, stat.st_ino]


def search(db, home, params, principal):
    query = params["query"]
    if not query.strip():
        raise ValueError("query is empty")
    with _search_snapshot(db, principal, home) as (conn, deadline, cancelled):
        try:
            return _search_page(db, conn, home, params, principal, deadline, cancelled)
        except sqlite3.OperationalError as exc:
            if str(exc) == "interrupted":
                raise LiveConflict("search superseded or budget exceeded; retry the page") from exc
            raise


def _search_page(db, conn, home, params, principal, deadline, cancelled):
    from hermex_search_pages import candidates
    profile, query = params["profile"], params["query"]
    target = params.get("session_id")
    target_chain = owned_lineage(db, target, profile, conn=conn) if target else None
    target_generation = lineage_generation(conn, target_chain) if target_chain else None
    scope = ["search", principal, profile, str(home), query, target, _store_identity(home)]
    if params.get("cursor"):
        phase, high_water, after, search_id = cursor_decode(params["cursor"], scope)
    else:
        phase, high_water, after = "messages", conn.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0], 0
        search_id = secrets.token_hex(16)
    revision_scope = _revision_scope(principal, profile, home)
    revision = digest([revision_scope, query, target, high_water, search_id])
    revision_key = (digest(revision_scope), revision)
    with _SEARCH_LOCK:
        observed = _REVISIONS.get(revision_key)
    if params.get("cursor") and observed is None:
        raise LiveConflict("search observation expired; search again")
    observed = dict(observed or {})
    limit = params.get("limit", 20)
    groups, cache, representatives, consumed, matched = {}, {}, {}, 0, 0
    if target_chain:
        cache.update({sid: target_chain for sid in target_chain})
    next_position = None
    if phase == "messages":
        prefix = db._sanitize_fts5_query(" ".join(
            token if token.startswith('"') or token.endswith("*") or token.upper() in {"AND", "OR", "NOT"}
            else token + "*" for token in re.findall(r'"[^"]*"|\S+', query.strip())))
        while consumed < 1000 and time.monotonic() < deadline - 0.15:
            batch = min(64, 1000 - consumed)
            try:
                hits, examined, exhausted = candidates(
                    db, conn, prefix, target_chain, after, high_water,
                    deadline=deadline, cancelled=cancelled, batch=batch)
                selected = _search_batch(db, conn, home, hits, profile, cache, representatives, query)
            except sqlite3.OperationalError as exc:
                if str(exc) != "interrupted" or cancelled.is_set():
                    raise
                next_position = ["messages", high_water, after, search_id]
                break
            for hit in hits:
                after = hit["id"]
                value = selected.get(hit["id"])
                if value is not None:
                    chain, match = value
                    group = groups.setdefault(chain[0], {"session_id": chain[-1], "lineage_root": chain[0],
                                                          "matches": [], "match_count": None})
                    group["matches"].append(match)
                    matched += 1
                if matched >= limit:
                    next_position = ["messages", high_water, after, search_id]
                    break
            if matched >= limit:
                break
            after = examined
            consumed += batch
            if exhausted:
                next_position = ["metadata", high_water, 0, search_id] if target is None else None
                break
            next_position = ["messages", high_water, after, search_id]
        else:
            next_position = ["messages", high_water, after, search_id]
        results = list(groups.values())
    else:
        results, next_offset = _metadata_search(db, conn, profile, query, after, limit)
        next_position = ["metadata", high_water, next_offset, search_id] if next_offset is not None else None
    if not results and next_position == ["metadata", high_water, 0, search_id]:
        results, next_offset = _metadata_search(db, conn, profile, query, 0, limit)
        next_position = ["metadata", high_water, next_offset, search_id] if next_offset is not None else None
    conn.set_progress_handler(None, 0)
    if cancelled.is_set():
        raise LiveConflict("search superseded")
    if db._db_file_was_replaced():
        raise LiveConflict("history store replaced; search again")
    generations = {digest(chain): index.generation for chain, index in representatives.items()}
    if target_chain:
        generations[digest(tuple(target_chain))] = target_generation
    generations.update(observed)
    with _SEARCH_LOCK:
        _REVISIONS[revision_key] = generations
        _REVISIONS.move_to_end(revision_key)
        while len(_REVISIONS) > 512:
            _REVISIONS.popitem(last=False)
    return {"version": 1, "profile": profile, "query": query, "revision": revision,
            "results": results, "next_cursor": cursor_encode(scope, next_position) if next_position else None}


def window(db, home, params, principal):
    from hermes_state_timeline import _snapshot
    with _snapshot(db) as conn:
        chain = owned_lineage(db, params["session_id"], params["profile"], conn=conn)
        revision_key = (digest(_revision_scope(principal, params["profile"], home)), params["expected_revision"])
        with _SEARCH_LOCK:
            generations = _REVISIONS.get(revision_key, {})
        generation = lineage_generation(conn, chain)
        if generations.get(digest(tuple(chain))) != generation:
            raise LiveConflict("history changed in this conversation; search again")
        anchor = int(params["message_id"])
        if anchor > 2**63 - 1:
            raise ValueError("message ID is outside the stored row-ID range")
        scope = ["window", principal, params["profile"], str(home), params["session_id"], str(anchor), generation]
        limit = params.get("limit", 100)
        index = [r["row_id"] for r in _lineage_rows(db, conn, chain)]
        if anchor not in index:
            raise ValueError("message unavailable or no longer representative")
        start = index.index(anchor)
        end = min(len(index), start + limit)
        if params.get("cursor"):
            start, end = cursor_decode(params["cursor"], scope)
        ids = index[start:end]
        rows = conn.execute("SELECT * FROM messages WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids).fetchall()
        by_id = {r["id"]: r for r in rows}
        messages = _display_messages(db, [by_id[i] for i in ids], home)
        if db._db_file_was_replaced():
            raise LiveConflict("history store replaced; search again")
    return {"version": 1, "profile": params["profile"], "session_id": chain[-1], "revision": params["expected_revision"],
            "anchor_message_id": str(anchor), "messages": messages, "offset": None,
            "older_cursor": cursor_encode(scope, [max(0, start - limit), start]) if start else None,
            "newer_cursor": cursor_encode(scope, [end, min(len(index), end + limit)]) if end < len(index) else None}
