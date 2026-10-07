import pytest

from hermes_state import SessionDB
from hermes_state_errors import SessionTurnLeaseLostError


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


def test_chain_delete_waits_for_delegate_on_earlier_segment(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("root", source="tui")
        db.end_session("root", "compression")
        db.create_session("tip", source="tui", parent_session_id="root")
        db.create_session("delegate", source="subagent", parent_session_id="root",
                          model_config={"_delegate_from": "root"})
        assert db.try_acquire_session_turn_lease("delegate", "foreign-delegate-holder")
        with pytest.raises(SessionTurnLeaseLostError):
            db.delete_sessions(["tip"], include_compression_chain=True)
        assert all(db.get_session(sid) is not None for sid in ("root", "tip", "delegate"))
        db.release_session_turn_lease("delegate", "foreign-delegate-holder")
        assert db.delete_sessions(["tip"], include_compression_chain=True) == 1
        assert all(db.get_session(sid) is None for sid in ("root", "tip", "delegate"))
    finally:
        db.close()
