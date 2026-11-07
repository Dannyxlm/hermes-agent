"""Family's physical-target lookup must not parse every frozen launch policy."""
import json
import sqlite3

from hermes_family import read_profile
from hermes_state import SessionDB


def test_policy_target_lookup_is_bounded_and_preserves_alias_owners(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(path) as db:
        db.create_session("tip", source="local")
        for logical, target, owner in (("logical-a", "tip", "alice"),
                                        ("logical-z", "tip", "zoe"),
                                        ("foreign", "other", "bob")):
            db.set_meta("gateway.local_policy.v1:" + logical, json.dumps({
                "entry": {"session_id": target}, "principal_id": owner, "policy": "x" * 10000}))
        db.set_meta("gateway.local_policy.v1:invalid", "not json")
        db.set_meta("unrelated", "not json")
    statements = []
    snapshot = read_profile("default", path, seeds=[("default", "tip")],
                            walk="lineage", trace=statements.append)
    assert snapshot.aliases == {"logical-a": "tip", "logical-z": "tip"}
    assert snapshot.owners == {"logical-a": "alice", "logical-z": "zoe", "tip": "zoe"}
    with sqlite3.connect(path) as conn:
        plans = [row[3] for sql in statements if "FROM state_meta" in sql
                 for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
    assert any("idx_state_meta_local_target" in plan and "<expr>=" in plan for plan in plans), plans


def test_policy_target_index_reconciles_and_tracks_in_place_rotation(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(path) as db:
        db.create_session("old", source="local")
        db.create_session("new", source="local")
        db.set_meta("gateway.local_policy.v1:logical", json.dumps({
            "entry": {"session_id": "old"}, "principal_id": "owner"}))
        db._conn.execute("DROP INDEX IF EXISTS idx_state_meta_local_target")
    with SessionDB(path) as db:
        indexes = {r[1] for r in db._conn.execute("PRAGMA index_list(state_meta)")}
        assert "idx_state_meta_local_target" in indexes
        db.set_meta("gateway.local_policy.v1:logical", json.dumps({
            "entry": {"session_id": "new"}, "principal_id": "owner"}))
    assert read_profile("default", path, seeds=[("default", "old")], walk="lineage").aliases == {}
    assert read_profile("default", path, seeds=[("default", "new")], walk="lineage").aliases == {"logical": "new"}
