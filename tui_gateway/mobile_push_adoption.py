"""Authenticated, installation-bounded adoption of the pre-issuer mobile principal."""

import hashlib


_OWNED = {
    "subscriptions": (("installation_id", "connection_id", "scope", "kind", "activity_id"), "expires_at"),
    "alert_policies": (("installation_id", "connection_id"), "expires_at"),
    "activity_start_tokens": (("installation_id", "connection_id"), "expires_at"),
    "human_origins": (("scope",), "updated_at"),
    "mobile_presence": (("installation_id", "connection_id", "scope"), "expires_at"),
    "widget_readers": (("installation_id", "connection_id"), "expires_at"),
    "widget_inbox_grants": (("installation_id", "connection_id"), None),
}


def _adopt_rows(db, table, keys, clock, legacy, principal, installation):
    order = f"{clock} DESC," if clock else ""
    rows = db.execute(f"""SELECT rowid AS adoption_rowid,* FROM {table}
        WHERE principal=? AND installation_id=? ORDER BY {order}rowid DESC""", (legacy, installation)).fetchall()
    match = " AND ".join(f"{key}=?" for key in keys)
    for row in rows:
        target = db.execute(f"SELECT rowid AS adoption_rowid,* FROM {table} WHERE principal=? AND {match}",
                            (principal, *(row[key] for key in keys))).fetchone()
        if target is not None:
            old_rank = (row[clock] if clock else 0, row["adoption_rowid"])
            new_rank = (target[clock] if clock else 0, target["adoption_rowid"])
            loser = row if old_rank <= new_rank else target
            db.execute(f"DELETE FROM {table} WHERE rowid=?", (loser["adoption_rowid"],))
            if loser is row:
                continue
        db.execute(f"UPDATE {table} SET principal=? WHERE rowid=?", (principal, row["adoption_rowid"]))


def adopt_legacy_installation(store, identity, principal, installation_id):
    """Only the same unfederated login can adopt its old digest, never another issuer."""
    from .mobile_push_store import normalized_uuid

    if store is None or identity.get("issuer"):
        return
    installation = normalized_uuid(installation_id)
    raw = f"{identity['provider']}\0{identity['user_id']}"
    legacy = "principal:dashboard:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    if legacy == principal:
        return
    union = " UNION ALL ".join(f"SELECT 1 FROM {table} WHERE principal=? AND installation_id=?" for table in _OWNED)
    with store._lock:
        if store._db.execute(f"SELECT 1 FROM ({union}) LIMIT 1", (legacy, installation) * len(_OWNED)).fetchone() is None:
            return
    with store.transaction() as db:
        # Reader/grant composite FKs have no ON UPDATE CASCADE. Move the pair atomically;
        # deleting a losing reader still cascades only its own obsolete grant.
        db.execute("PRAGMA defer_foreign_keys=ON")
        for table, (keys, clock) in _OWNED.items():
            _adopt_rows(db, table, keys, clock, legacy, principal, installation)
        db.execute("""DELETE FROM alert_policies WHERE rowid IN (
            SELECT rowid FROM (SELECT rowid,ROW_NUMBER() OVER (
                PARTITION BY token,environment ORDER BY expires_at DESC,rowid DESC) AS rank
                FROM alert_policies WHERE principal=? AND installation_id=?) WHERE rank>1)""", (principal, installation))


def adopt_mobile_identity(home, service, identity, principal, installation):
    if service is not None:
        adopt_legacy_installation(service.store, identity, principal, installation)
    else:
        from .mobile_push import _offline_store
        _offline_store(home, lambda store: adopt_legacy_installation(store, identity, principal, installation))


def unregister_installation(db, principal, installation):
    """Logout ends this human's enrollment on the installation, including sibling connections."""
    count = db.execute("DELETE FROM subscriptions WHERE principal=? AND installation_id=?", (principal, installation)).rowcount
    for table in _OWNED:
        if table != "subscriptions":
            db.execute(f"DELETE FROM {table} WHERE principal=? AND installation_id=?", (principal, installation))
    return count
