"""Read-only Hermex catalog snapshots and exact, revision-bound history pages."""
from __future__ import annotations

import base64
from collections import OrderedDict
from datetime import UTC, datetime
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import threading

from hermes_state_messages import DISPLAY_VISIBLE_SQL

_CURSOR_KEY = secrets.token_bytes(32)
_CATALOGS = OrderedDict()
_CATALOG_LOCK = threading.Lock()


class LiveConflict(ValueError):
    """The caller's snapshot no longer names the current state."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cursor_encode(scope, position):
    data = json.dumps([scope, position], sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(hmac.digest(_CURSOR_KEY, data, "sha256") + data).decode()


def cursor_decode(token, scope):
    try:
        raw = base64.b64decode(token, altchars=b"-_", validate=True)
        signature, data = raw[:32], raw[32:]
        if not hmac.compare_digest(signature, hmac.digest(_CURSOR_KEY, data, "sha256")):
            raise ValueError("signature")
        actual, position = json.loads(data)
        if actual != scope:
            raise ValueError("scope")
        return position
    except (ValueError, TypeError, UnicodeError) as exc:
        raise LiveConflict("cursor expired or scope changed") from exc


def store_revision(home):
    stamps = []
    for path in (home / "state.db", home / "state.db-wal"):
        try:
            stat = path.stat()
            stamps.append([stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns])
        except FileNotFoundError:
            stamps.append(None)
    return digest(stamps)


def owned_lineage(db, session_id, profile):
    from hermes_state_sessions import INTERNAL_LISTING_SOURCES
    chain = db.get_compression_chain(session_id)
    if session_id not in chain:
        raise ValueError("session unavailable")
    for sid in chain:
        row = db.get_session(sid)
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
            records.append([file.relative_to(root).as_posix(), hashlib.sha256(file.read_bytes()).hexdigest(),
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
    """The REST lineage identity rule, selecting only representative and logical IDs."""
    names = ('role', 'content', 'timestamp', 'tool_call_id', 'tool_calls', 'tool_name',
             'display_kind', 'display_metadata')

    def identity(*values):
        return db._display_identity(db._display_dedupe_key(dict(zip(names, values))))

    conn.create_function("hermex_identity", len(names), identity, deterministic=True)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    computed = f"hermex_identity({', '.join(names)})"
    partition = f"COALESCE(display_identity, {computed})" if 'display_identity' in columns else computed
    return f"""WITH identities AS (
        SELECT id, active, {partition} AS identity FROM messages
        WHERE session_id IN ({','.join('?' for _ in chain)})
          AND (active=1 OR compacted=1){DISPLAY_VISIBLE_SQL}
    ), display_rows AS (
        SELECT COALESCE(MAX(CASE WHEN active=1 THEN id END), MAX(id)) AS row_id,
               MIN(id) AS logical_id FROM identities GROUP BY identity
    )"""


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
    hits = list(pattern.finditer(text)) if pattern else []
    start = max(0, hits[0].start() - 120) if hits else 0
    snippet = text[start:start + 400]
    ranges = [{"start": len(snippet[:m.start()].encode("utf-16-le")) // 2,
               "length": len(m.group().encode("utf-16-le")) // 2} for m in pattern.finditer(snippet)] if pattern else []
    return snippet, ranges


def _search_batch(db, home, hits, profile, target_chain, cache, query):
    batches = {}
    for hit in hits:
        sid = hit["session_id"]
        if target_chain is not None and sid not in target_chain:
            continue
        if sid not in cache:
            try:
                cache[sid] = owned_lineage(db, sid, profile)
            except ValueError:
                cache[sid] = None
        if cache[sid] is not None:
            batches.setdefault(tuple(cache[sid]), []).append(hit)
    selected = {}
    for chain, candidates in batches.items():
        ids = [hit["id"] for hit in candidates]
        with db._read_ctx() as conn:
            sql = lineage_rows_sql(db, conn, chain)
            rows = conn.execute(sql + "SELECT m.*, d.logical_id AS hermex_logical_id FROM display_rows d "
                                "JOIN messages m ON m.id=d.row_id WHERE d.row_id IN ("
                                + ",".join("?" for _ in ids) + ")", (*chain, *ids)).fetchall()
        logical_ids = {row["id"]: str(row["hermex_logical_id"]) for row in rows}
        for message in _display_messages(db, rows, home):
            if message.get("display_kind") == "hidden":
                continue
            snippet, ranges = _snippet(message, query)
            # A model-facing summary may match while its display projection does not.
            if "display_content" in message and not ranges:
                continue
            selected[message["id"]] = (chain, {
                "message_id": str(message["id"]), "logical_id": logical_ids[message["id"]],
                "source_session_id": message["session_id"], "role": message["role"],
                "snippet": snippet, "ranges": ranges})
    return selected


def _metadata_search(db, profile, query, offset, limit):
    pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = db._read_all("SELECT id FROM sessions WHERE (id LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\') "
                        "ORDER BY id LIMIT ? OFFSET ?", (pattern, pattern, limit + 1, offset))
    groups = {}
    for row in rows[:limit]:
        try:
            chain = owned_lineage(db, row["id"], profile)
        except ValueError:
            continue
        groups[chain[0]] = {"session_id": chain[-1], "lineage_root": chain[0], "matches": [], "match_count": None}
    return list(groups.values()), offset + limit if len(rows) > limit else None


def search(db, home, params, principal):
    profile, query = params["profile"], params["query"]
    if not query.strip():
        raise ValueError("query is empty")
    revision = store_revision(home)
    target = params.get("session_id")
    target_chain = owned_lineage(db, target, profile) if target else None
    scope = ["search", principal, profile, str(home), query, target, revision]
    phase, offset = cursor_decode(params["cursor"], scope) if params.get("cursor") else ("messages", 0)
    limit = params.get("limit", 20)
    groups, cache, consumed = {}, {}, 0
    next_position = None
    if phase == "messages":
        prefix = " ".join(token if token.startswith('"') or token.endswith("*") else token + "*"
                          for token in re.findall(r'"[^"]*"|\S+', query.strip()))
        # Bound work per request as well as payload. Empty pages can still have a next cursor.
        while consumed < 1000:
            hits = db.search_messages(prefix, role_filter=["user", "assistant"], limit=100, offset=offset,
                                      sort="oldest", fields=("id", "session_id", "role"))
            selected = _search_batch(db, home, hits, profile, target_chain, cache, query)
            for hit in hits:
                offset += 1
                consumed += 1
                match = selected.get(hit["id"])
                if match is not None:
                    chain, value = match
                    group = groups.setdefault(chain[0], {"session_id": chain[-1], "lineage_root": chain[0],
                                                          "matches": [], "match_count": None})
                    group["matches"].append(value)
                if sum(len(g["matches"]) for g in groups.values()) >= limit or consumed >= 1000:
                    next_position = ["messages", offset]
                    break
            if next_position is not None:
                break
            if len(hits) < 100:
                next_position = ["metadata", 0] if target is None else None
                break
        results = list(groups.values())
    else:
        results, next_offset = _metadata_search(db, profile, query, offset, limit)
        next_position = ["metadata", next_offset] if next_offset is not None else None
    if not results and next_position == ["metadata", 0]:
        results, next_offset = _metadata_search(db, profile, query, 0, limit)
        next_position = ["metadata", next_offset] if next_offset is not None else None
    if store_revision(home) != revision:
        raise LiveConflict("history changed during search; search again")
    return {"version": 1, "profile": profile, "query": query, "revision": revision,
            "results": results, "next_cursor": cursor_encode(scope, next_position) if next_position else None}


def window(db, home, params, principal):
    revision = store_revision(home)
    if params["expected_revision"] != revision:
        raise LiveConflict("history changed; search again")
    chain = owned_lineage(db, params["session_id"], params["profile"])
    anchor = int(params["message_id"])
    if anchor > 2**63 - 1:
        raise ValueError("message ID is outside the stored row-ID range")
    scope = ["window", principal, params["profile"], str(home), params["session_id"], str(anchor), revision]
    limit = params.get("limit", 100)
    with db._read_ctx() as conn:
        sql = lineage_rows_sql(db, conn, chain)
        row = conn.execute(sql + "SELECT logical_id FROM display_rows WHERE row_id=?", (*chain, anchor)).fetchone()
        if row is None:
            raise ValueError("message unavailable or no longer representative")
        start = conn.execute(sql + "SELECT COUNT(*) FROM display_rows WHERE logical_id < ?",
                             (*chain, row[0])).fetchone()[0]
        if params.get("cursor"):
            start = cursor_decode(params["cursor"], scope)
        total = conn.execute(sql + "SELECT COUNT(*) FROM display_rows", chain).fetchone()[0]
        rows = conn.execute(sql + "SELECT m.* FROM (SELECT row_id, logical_id FROM display_rows "
                            "ORDER BY logical_id LIMIT ? OFFSET ?) page JOIN messages m ON m.id=page.row_id "
                            "ORDER BY page.logical_id", (*chain, limit, start)).fetchall()
    messages = _display_messages(db, rows, home)
    if store_revision(home) != revision:
        raise LiveConflict("history changed during window read; search again")
    return {"version": 1, "profile": params["profile"], "session_id": chain[-1], "revision": revision,
            "anchor_message_id": str(anchor), "messages": messages, "offset": None,
            "older_cursor": cursor_encode(scope, max(0, start - limit)) if start else None,
            "newer_cursor": cursor_encode(scope, start + len(rows)) if start + len(rows) < total else None}
