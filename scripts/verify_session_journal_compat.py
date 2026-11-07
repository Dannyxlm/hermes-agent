#!/usr/bin/env python3
"""Synthetic prior -> candidate -> prior -> candidate compatibility for the session journal.

Only disposable work-dir state is written. Run with an empty-secret environment and
throwaway HOME. This is source compatibility evidence, not a production backup or
an artifact-bound release-controller receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def schema_hash(conn):
    rows = [tuple(row) for row in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def prior_first(db):
    db.create_session("prior", "desktop")
    db.append_message("prior", "user", "synthetic prior message")


def candidate_first(db):
    assert len(db.get_messages("prior")) == 1
    db.create_session("candidate", "desktop")
    db.append_message("candidate", "user", "synthetic candidate message")
    db.create_session("delete-me", "desktop")
    db.create_session("prune-me", "cron")
    db.end_session("prune-me", "done")
    db._conn.execute("UPDATE sessions SET started_at=1,ended_at=2 WHERE id='prune-me'")


def prior_second(db):
    assert len(db.get_messages("prior")) == len(db.get_messages("candidate")) == 1
    db.append_message("prior", "assistant", "synthetic rollback write")
    assert db.delete_session("delete-me")
    assert db.prune_sessions(older_than_days=90, source="cron") == 1


def candidate_second(db):
    assert len(db.get_messages("prior")) == 2
    assert len(db.get_messages("candidate")) == 1
    assert db.get_session("delete-me") is None and db.get_session("prune-me") is None
    db.append_message("candidate", "assistant", "synthetic re-upgrade write")
    assert [tuple(row) for row in db._conn.execute("PRAGMA quick_check")] == [("ok",)]
    assert db._conn.execute("PRAGMA foreign_key_check").fetchall() == []


_STAGES = {"prior-first": prior_first, "candidate-first": candidate_first,
           "prior-second": prior_second, "candidate-second": candidate_second}


def stage(args):
    sys.path.insert(0, str(args.root.resolve()))
    from hermes_state import SessionDB
    path = args.work_dir / "state.db"
    if args.stage == "candidate-first":
        from hermes_state_reconcile import schema_drift_reason
        assert schema_drift_reason(path) is None, "optional optimization must not block legacy read-only browsing"
    with SessionDB(db_path=path) as db:
        _STAGES[args.stage](db)
        has_journal = db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='session_projection_changes'").fetchone() is not None
        high = (db._conn.execute("SELECT MAX(seq) FROM session_projection_changes").fetchone()[0]
                if has_journal else None)
        return {"stage": args.stage, "schema_hash": schema_hash(db._conn), "journal_high": high,
                "sessions": db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "messages": db._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]}


def run(args):
    args.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (args.work_dir / "state.db").exists():
        raise ValueError("work-dir must not contain a prior state.db")
    results = []
    for name, root in (("prior-first", args.prior_root), ("candidate-first", args.candidate_root),
                       ("prior-second", args.prior_root), ("candidate-second", args.candidate_root)):
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--stage", name,
                                "--root", str(root.resolve()), "--work-dir", str(args.work_dir.resolve())],
                               check=True, capture_output=True, text=True, cwd=root, timeout=120)
        results.append(json.loads(child.stdout))
    assert len({result["schema_hash"] for result in results[1:]}) == 1
    assert results[1]["journal_high"] < results[2]["journal_high"] < results[3]["journal_high"]
    return {"status": "passed", "compatibility": "selector_only_backward_compatible", "stages": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prior-root", type=Path)
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--stage", choices=("prior-first", "candidate-first", "prior-second", "candidate-second"))
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    if args.stage and not args.root:
        parser.error("stage requires root")
    if not args.stage and (not args.prior_root or not args.candidate_root):
        parser.error("prior-root and candidate-root are required")
    print(json.dumps(stage(args) if args.stage else run(args), indent=2))


if __name__ == "__main__":
    main()
