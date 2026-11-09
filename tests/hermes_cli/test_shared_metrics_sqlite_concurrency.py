"""Shared-metrics readers must not stall the independent consent/export/sender writers."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from hermes_cli.observability.relay_shared_metrics import _reconcile_store_consent
from hermes_cli.observability.shared_metrics import SharedMetricsStore
from hermes_cli.observability.shared_metrics_sender import SharedMetricsSender, reconcile_send_consent
from hermes_state_wal import is_sqlite_wal_reset_vulnerable


@pytest.mark.skipif(is_sqlite_wal_reset_vulnerable(), reason="WAL is unsafe on the linked SQLite")
@pytest.mark.parametrize("operation", ["consent", "export", "claim"])
def test_shared_metrics_writers_progress_while_a_reader_is_paused(tmp_path, operation):
    store = SharedMetricsStore(tmp_path / "metrics.sqlite3", tmp_path / "outbox")
    now = datetime(2026, 8, 26, tzinfo=UTC)
    with store._write() as connection:
        reconcile_send_consent(connection, True, now=datetime(2026, 8, 24, tzinfo=UTC))
        reconcile_send_consent(connection, True, now=now)
        for index in range(3):
            package_id = f"package-{index}"
            payload = {"package_id": package_id, "install_id": "test-install"}
            connection.execute(
                "INSERT INTO package_outbox(package_id, period_start, period_end, payload_json,"
                " created_at, exported_at) VALUES (?, ?, ?, ?, ?, ?)",
                (package_id, "2026-08-25T00:00:00Z", "2026-08-26T00:00:00Z",
                 json.dumps(payload), "2026-08-26T00:00:00Z",
                 "2026-08-26T00:00:00Z" if operation == "claim" else None),
            )

    # An unfinished SELECT holds a real read lock in rollback-journal mode, just as a
    # snapshot/export read paused by another thread or process does. No timing race needed.
    with store._connection() as reader:
        cursor = reader.execute("SELECT package_id FROM package_outbox ORDER BY package_id")
        assert cursor.fetchone()[0] == "package-0"
        try:
            if operation == "consent":
                _reconcile_store_consent(store, False)
                with store._connection() as check:
                    assert check.execute(
                        "SELECT closed_at FROM send_consent_windows"
                    ).fetchone()[0] is not None
            elif operation == "export":
                paths = store._export_pending_packages()
                assert {path.stem for path in paths} == {f"package-{i}" for i in range(3)}
                assert all(json.loads(path.read_text())["install_id"] == "test-install" for path in paths)
            else:
                sender = SharedMetricsSender(store, "https://telemetry.test/v1/telemetry")
                claimed = sender._claim_next(now, set())
                assert claimed is not None and claimed["package_id"] == "package-0"
                assert claimed["claim_token"] and not claimed["skip"]
            assert [row[0] for row in cursor.fetchall()] == ["package-1", "package-2"]
        finally:
            cursor.close()


@pytest.mark.parametrize("fallback", ["configured-delete", "vulnerable-sqlite"])
def test_shared_metrics_keeps_canonical_rollback_journal_safety(tmp_path, monkeypatch, fallback):
    import hermes_state_wal

    monkeypatch.setattr(hermes_state_wal, "resolve_journal_mode", lambda: (
        "delete" if fallback == "configured-delete" else "wal"
    ))
    monkeypatch.setattr(hermes_state_wal, "is_sqlite_wal_reset_vulnerable", lambda: (
        fallback == "vulnerable-sqlite"
    ))
    store = SharedMetricsStore(tmp_path / "metrics.sqlite3", tmp_path / "outbox")
    with store._connection() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 250
    with sqlite3.connect(store.database_path) as check:
        assert check.execute("SELECT value FROM telemetry_state WHERE key = 'schema_version'").fetchone()
