"""Page representatives without transferring indexed lineage payloads to Python."""


def lineage_display_page(self, session_ids, *, active_clause, limit, offset=0, latest=False,
                          drop_removed=False):
    """Fold the lineage's identities before loading page payloads, within one read snapshot.

    Removed copies hide ancestors by identity or absorbed uid; summary carriers fold by uid.
    Current stores need no bodies for this pass; legacy rows compute their missing identity.
    """
    from hermes_state_messages import (
        DISPLAY_VISIBLE_SQL, MODEL_ONLY_DISPLAY_METADATA_KEY, _placeholders, _sql_json_extract)

    def read(conn):
        own_snapshot = not conn.in_transaction
        if own_snapshot:
            conn.execute('BEGIN')
        try:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(messages)')}
            stored = 'display_identity' if 'display_identity' in columns else 'NULL'
            removed = drop_removed and conn.execute(
                f"SELECT 1 FROM messages WHERE session_id IN ({_placeholders(session_ids)}) "
                "AND active=0 AND compacted=0 LIMIT 1", session_ids).fetchone()
            if not removed:
                return indexed_lineage_page(self, conn, session_ids, active_clause=active_clause,
                    stored=stored, limit=limit, offset=offset, latest=latest, visible_sql=DISPLAY_VISIBLE_SQL)
            candidates = conn.execute(
                f"SELECT id, role, timestamp, tool_call_id, tool_name, display_kind, "
                f"CASE WHEN {stored} IS NULL THEN tool_calls END AS tool_calls, "
                f"CASE WHEN {stored} IS NULL THEN display_metadata END AS display_metadata, "
                f"{_sql_json_extract('display_metadata', '$.' + MODEL_ONLY_DISPLAY_METADATA_KEY)} AS model_only, "
                f"message_uid, absorbed_message_uids, _compressed_summary, active, compacted, "
                f"{stored} AS display_identity, CASE WHEN {stored} IS NULL THEN content END AS content "
                f"FROM messages WHERE session_id IN ({_placeholders(session_ids)})"
                f"{'' if drop_removed else active_clause} ORDER BY id", session_ids)
            rows = self._dedupe_display_generations(
                list(candidates), drop_removed=drop_removed, stored_identities=True)
            rows = rows[::-1][offset:][:limit][::-1] if latest else rows[offset:][:limit]
            selected_ids = [row['id'] for row in rows]
            selected = {}
            for start in range(0, len(selected_ids), 900):
                chunk = selected_ids[start:start + 900]
                selected.update({row['id']: row for row in conn.execute(
                    f"SELECT * FROM messages WHERE id IN ({_placeholders(chunk)})", chunk)})
            return [selected[row_id] for row_id in selected_ids]
        finally:
            if own_snapshot and conn.in_transaction:
                conn.execute('ROLLBACK')

    return self._read_retrying_ioerr(read)


def indexed_lineage_page(db, conn, session_ids, *, active_clause, stored, limit, offset, latest,
                         visible_sql):
    """No removed rows: SQL groups copies and folds preceding summary carriers before paging.

    Legacy identities run inside SQLite, as on single-segment reads; only the chosen page is
    returned as body-bearing rows. Removed-copy suppression uses the shared Python fold instead.
    """
    names = ('role', 'content', 'timestamp', 'tool_call_id', 'tool_calls', 'tool_name',
             'display_kind', 'display_metadata')

    def identity(*values):
        return db._display_identity(db._display_dedupe_key(dict(zip(names, values))))

    conn.create_function('lineage_display_identity', len(names), identity, deterministic=True)
    computed = f"lineage_display_identity({', '.join(names)})"
    direction = 'DESC' if latest else 'ASC'
    return conn.execute(f"""
        WITH eligible AS MATERIALIZED (
            SELECT id, active, message_uid, _compressed_summary,
                   COALESCE({stored}, {computed}) AS identity
            FROM messages WHERE session_id IN ({','.join('?' for _ in session_ids)})
                {active_clause}{visible_sql}
        ), first_uids AS (
            SELECT message_uid, MIN(id) AS id FROM eligible
            WHERE COALESCE(_compressed_summary, 0) = 0 AND message_uid IS NOT NULL AND message_uid != ''
            GROUP BY message_uid
        ), folded AS (
            SELECT candidate.id, candidate.active,
                CASE WHEN candidate._compressed_summary AND original.id < candidate.id
                     THEN original.identity ELSE candidate.identity END AS identity
            FROM eligible candidate LEFT JOIN first_uids first ON first.message_uid = candidate.message_uid
                LEFT JOIN eligible original ON original.id = first.id
        ), page AS (
            SELECT COALESCE(MAX(CASE WHEN active = 1 THEN id END), MAX(id)) AS id,
                MIN(id) AS logical_id
            FROM folded GROUP BY identity
            ORDER BY logical_id {direction} LIMIT ? OFFSET ?
        )
        SELECT chosen.* FROM page JOIN messages chosen ON chosen.id = page.id
        ORDER BY page.logical_id ASC
        """, (*session_ids, -1 if limit is None else limit, offset)).fetchall()
