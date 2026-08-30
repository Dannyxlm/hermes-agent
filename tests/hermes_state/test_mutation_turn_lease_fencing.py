import pytest

from hermes_state import SessionDB, SessionTurnLeaseLostError


def test_transcript_replace_rejects_reclaimed_webui_holder(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("shared", source="webui")
        db.append_message("shared", "user", "original")
        old_holder = "webui-mutation:old"
        new_holder = "desktop-turn:new"
        assert db.try_acquire_session_turn_lease("shared", old_holder)
        db.release_session_turn_lease("shared", old_holder)
        assert db.try_acquire_session_turn_lease("shared", new_holder)

        with pytest.raises(SessionTurnLeaseLostError):
            db.replace_messages(
                "shared",
                [{"role": "user", "content": "stale replacement"}],
                active_only=True,
                archive_dropped=True,
                turn_lease_holder=old_holder,
            )

        assert [row["content"] for row in db.get_messages("shared")] == [
            "original"
        ]
    finally:
        db.close()


def test_transcript_replace_accepts_its_owned_lease_with_rejection_enabled(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("shared", source="webui")
        db.append_message("shared", "user", "original")
        holder = "webui-mutation:current"
        assert db.try_acquire_session_turn_lease("shared", holder)

        db.replace_messages(
            "shared",
            [{"role": "user", "content": "replacement"}],
            active_only=True,
            archive_dropped=True,
            reject_active_turn_lease=True,
            turn_lease_holder=holder,
        )

        assert [row["content"] for row in db.get_messages("shared")] == [
            "replacement"
        ]
    finally:
        db.close()


def test_cancel_rollback_atomically_revokes_the_stale_writer(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    session_id = "cancelled-turn"
    holder = "webui-turn:123:cancel"
    before = [{"role": "user", "content": "older"}]
    current = [*before, {"role": "user", "content": "cancel me"}]
    try:
        db.create_session(session_id, source="webui")
        db.replace_messages(session_id, current)
        assert db.try_acquire_session_turn_lease(session_id, holder)

        db.replace_messages(
            session_id,
            before,
            active_only=True,
            turn_lease_holder=holder,
            revoke_turn_lease=True,
        )

        assert [
            message["content"]
            for message in db.get_messages_as_conversation(session_id)
        ] == ["older"]
        assert db.refresh_session_turn_lease(session_id, holder) is False
        with pytest.raises(SessionTurnLeaseLostError):
            db.append_message(
                session_id,
                "assistant",
                "late finalizer",
                turn_lease_holder=holder,
            )
        assert [
            message["content"]
            for message in db.get_messages_as_conversation(session_id)
        ] == ["older"]
    finally:
        db.close()


def test_in_place_compression_rejects_a_reclaimed_turn_holder(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("shared", source="webui")
        db.append_message("shared", "user", "original")
        old_holder = "webui-turn:old"
        assert db.try_acquire_session_turn_lease("shared", old_holder)
        db.release_session_turn_lease("shared", old_holder)
        assert db.try_acquire_session_turn_lease("shared", "desktop-turn:new")

        with pytest.raises(SessionTurnLeaseLostError):
            db.archive_and_compact(
                "shared",
                [{"role": "assistant", "content": "stale summary"}],
                turn_lease_holder=old_holder,
            )

        assert [row["content"] for row in db.get_messages("shared")] == [
            "original"
        ]
    finally:
        db.close()


def test_rotation_compression_rejects_a_reclaimed_turn_holder(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("parent", source="webui")
        db.append_message("parent", "user", "original")
        old_holder = "webui-turn:old"
        assert db.try_acquire_session_turn_lease("parent", old_holder)
        db.release_session_turn_lease("parent", old_holder)
        assert db.try_acquire_session_turn_lease("parent", "desktop-turn:new")

        with pytest.raises(SessionTurnLeaseLostError):
            db.publish_compression_child(
                parent_session_id="parent",
                child_session_id="stale-child",
                source="webui",
                messages=[{"role": "assistant", "content": "stale summary"}],
                require_compression_lease=False,
                turn_lease_holder=old_holder,
            )

        assert db.get_session("parent")["ended_at"] is None
        assert db.get_session("stale-child") is None
    finally:
        db.close()
