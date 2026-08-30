import pytest

from hermes_state import SessionDB, SessionTurnLeaseLostError


def test_parent_delete_waits_for_active_persistent_delegate(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("parent", source="webui")
        db.create_session(
            "delegate",
            source="subagent",
            parent_session_id="parent",
            model_config={"_delegate_from": "parent"},
        )
        parent_holder = "webui-turn:parent"
        child_holder = "foreign-delegate-holder"
        assert db.try_acquire_session_turn_lease("parent", parent_holder)
        assert db.try_acquire_session_turn_lease("delegate", child_holder)

        with pytest.raises(SessionTurnLeaseLostError):
            db.delete_sessions(
                ["parent"],
                turn_lease_holder=parent_holder,
            )
        assert db.get_session("parent") is not None
        assert db.get_session("delegate") is not None

        db.release_session_turn_lease("delegate", child_holder)
        assert db.delete_sessions(
            ["parent"],
            turn_lease_holder=parent_holder,
        ) == 1
        assert db.get_session("parent") is None
        assert db.get_session("delegate") is None
    finally:
        db.close()
