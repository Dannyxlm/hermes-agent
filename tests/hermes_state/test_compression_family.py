from hermes_state import SessionDB


def test_compression_family_includes_stale_sibling_but_keeps_live_tip_last(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("root", source="webui")
        db.end_session("root", "compression")
        db.create_session("stale", source="webui", parent_session_id="root")
        db.end_session("stale", "ws_orphan_reap")
        db.create_session("live", source="webui", parent_session_id="root")

        assert db.get_compression_tip("root") == "live"
        assert db.get_compression_tip("stale") == "live"
        assert db.get_compression_lineage("root") == ["root", "live"]
        assert db.get_compression_lineage("stale") == ["root", "live"]
        assert db.get_compression_family("root") == ["root", "stale", "live"]
        assert db.get_compression_family("stale") == ["root", "stale", "live"]
    finally:
        db.close()


def test_compression_tip_walks_beyond_legacy_depth_limit(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        root = "segment-000"
        db.create_session(root, source="webui")
        current = root
        for index in range(1, 151):
            db.end_session(current, "compression")
            child = f"segment-{index:03d}"
            db.create_session(child, source="webui", parent_session_id=current)
            current = child

        assert db.get_compression_tip(root) == current
        lineage = db.get_compression_lineage(root)
        assert len(lineage) == 151
        assert lineage[-1] == current
    finally:
        db.close()
