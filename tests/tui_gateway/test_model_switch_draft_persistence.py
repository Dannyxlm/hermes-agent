"""A model switch before the first prompt must persist its timeline marker."""

from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tui_gateway import server


@pytest.mark.parametrize("prebuilt_agent", [False, True])
def test_model_switch_persists_before_first_prompt(monkeypatch, tmp_path, prebuilt_agent):
    with SessionDB(db_path=tmp_path / "state.db") as db:
        monkeypatch.setattr(server, "_get_db", lambda: db)
        monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
        monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
        monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
        monkeypatch.setattr(server, "_session_default_route", lambda _session: ("test-model", "test-provider"))
        response = server.handle_request({
            "id": "create", "method": "session.create", "params": {"source": "desktop"}})
        assert "result" in response, response
        sid = response["result"]["session_id"]
        session = server._sessions[sid]
        key = session["session_key"]
        try:
            # session.create deliberately leaves empty drafts out of the store. Building
            # the agent opens its DB handle, but its session row is still lazy.
            assert db.get_session(key) is None
            if prebuilt_agent:
                session["agent"] = SimpleNamespace(_session_db=db, session_id=key)

            server._append_model_switch_marker(session, model="next-model", provider="test-provider")

            rows = db.get_messages(key)
            assert len(rows) == 1
            assert rows[0]["display_kind"] == "model_switch"
            assert rows[0]["role"] == "user"
            assert "next-model" in rows[0]["content"]
            assert session["history"][-1]["_row_id"] == rows[0]["id"]
            assert session["history"][-1]["_db_persisted"] is True
        finally:
            server._sessions.pop(sid, None)
