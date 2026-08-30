from hermes_state import SessionDB


def test_archive_session_messages_retires_lineage_without_deleting_sessions(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("root", source="webui")
        db.append_message("root", "user", "older")
        db.end_session("root", "compression")
        db.create_session("tip", source="webui", parent_session_id="root")
        db.append_message("tip", "assistant", "newer")
        db.create_session(
            "delegate",
            source="subagent",
            parent_session_id="root",
            model_config={"_delegate_from": "root"},
        )
        db.append_message("delegate", "assistant", "delegated result")

        assert db.archive_session_messages(["root", "tip"]) == 2

        assert db.get_messages("root") == []
        assert db.get_messages("tip") == []
        assert [row["content"] for row in db.get_messages("root", include_inactive=True)] == [
            "older"
        ]
        assert [row["content"] for row in db.get_messages("tip", include_inactive=True)] == [
            "newer"
        ]
        assert db.get_session("root") is not None
        assert db.get_session("tip") is not None
        assert db.get_session("delegate") is not None
        assert [row["content"] for row in db.get_messages("delegate")] == [
            "delegated result"
        ]

        db.replace_messages(
            "tip",
            [{"role": "user", "content": "retained prefix"}],
            active_only=True,
            archive_dropped=True,
        )
        model_history, display_history = db.get_resume_conversations("tip")
        assert [row["content"] for row in model_history] == ["retained prefix"]
        assert [row["content"] for row in display_history] == ["retained prefix"]
    finally:
        db.close()
