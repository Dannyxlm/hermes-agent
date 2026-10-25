"""Device-level registration ownership and an outbox uniqueness backstop."""


_DEVICE_KEYS = {
    "subscriptions": ("installation_id", "token", "environment", "scope", "kind", "activity_id"),
}


def migrate_device_keys(db):
    """Run inside the store's startup transaction, including on predecessor-created stores."""
    for table, columns in _DEVICE_KEYS.items():
        index = f"{table}_device_key"
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (index,)).fetchone():
            db.execute("""DELETE FROM subscriptions WHERE kind='activity_start' AND EXISTS (
                SELECT 1 FROM activity_start_tokens t WHERE t.installation_id=subscriptions.installation_id
                AND t.token=subscriptions.token AND t.environment=subscriptions.environment
                AND t.principal!=subscriptions.principal)""")
            # Legacy leases have no registration timestamp. Expiry is the renewal clock;
            # rowid breaks ties (ActivityKit leases can share a run's fixed expiry).
            db.execute(f"""DELETE FROM {table} WHERE rowid IN (
                SELECT rowid FROM (SELECT rowid, ROW_NUMBER() OVER (
                    PARTITION BY {', '.join(columns)} ORDER BY expires_at DESC, rowid DESC) AS rank
                    FROM {table} WHERE token!='') WHERE rank>1)""")
            db.execute(f"CREATE UNIQUE INDEX {index} ON {table} ({', '.join(columns)}) WHERE token!=''")
        match = " AND ".join(f"{column}=NEW.{column}" for column in columns)
        own = """id=NEW.id OR (principal=NEW.principal AND installation_id=NEW.installation_id
            AND connection_id=NEW.connection_id AND scope=NEW.scope AND kind=NEW.kind AND activity_id=NEW.activity_id)"""
        for action in ("INSERT", "UPDATE OF " + ", ".join(columns)):
            suffix = "insert" if action == "INSERT" else "update"
            db.execute(f"DROP TRIGGER IF EXISTS {index}_{suffix}")
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS {index}_v2_{suffix}
                BEFORE {action} ON {table} WHEN NEW.token!=''
                BEGIN
                    DELETE FROM {table} WHERE {match} AND NOT ({own});
                END""")
    for table in ("alert_policies", "activity_start_tokens"):
        for suffix in ("insert", "update"):
            db.execute(f"DROP TRIGGER IF EXISTS {table}_device_key_{suffix}")
        db.execute(f"DROP INDEX IF EXISTS {table}_device_key")
    db.execute("CREATE INDEX IF NOT EXISTS outbox_event ON outbox(event_id)")
    # Every producer inserts here, including widget reloads and ActivityKit cleanup.
    # Unlike per-subscription uniqueness, this also covers re-minted installation IDs.
    db.execute("""CREATE TRIGGER IF NOT EXISTS outbox_device_event
        BEFORE INSERT ON outbox WHEN EXISTS (
            SELECT 1 FROM outbox o JOIN subscriptions old ON old.id=o.subscription_id
            JOIN subscriptions target ON target.id=NEW.subscription_id
            WHERE o.event_id=NEW.event_id AND old.token=target.token
                AND old.environment=target.environment AND target.token!='')
        BEGIN SELECT RAISE(IGNORE); END""")
