"""Family overview selection stays indexed after reopening an older store."""
from hermes_state import SessionDB


def test_overview_launch_indexes_reconcile_and_bound_window_reads(tmp_path):
    path = tmp_path / "state.db"
    indexes = {"idx_work_launches_finished", "idx_work_launches_restart"}
    with SessionDB(path) as db:
        db.create_session("root", source="test")
        for index in indexes:
            db._conn.execute(f"DROP INDEX IF EXISTS {index}")
        db._conn.commit()
    with SessionDB(path) as db:
        assert indexes <= {row[1] for row in db._conn.execute("PRAGMA index_list(work_launches)")}
        from hermes_family import read_profile

        statements = []
        read_profile("default", path, since=100, trace=statements.append)
        windowed = [sql for sql in statements if "WHERE finished_at IS NULL OR finished_at>=" in sql]
        assert windowed
        for sql in windowed:
            plan = [row[3] for row in db._conn.execute("EXPLAIN QUERY PLAN " + sql)]
            assert any("MULTI-INDEX OR" in line for line in plan), plan
            assert all(any(index in line for line in plan) for index in indexes), plan
            assert not any("SCAN work_launches" in line for line in plan), plan
