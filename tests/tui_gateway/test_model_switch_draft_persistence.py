"""A model switch before the first prompt must persist its timeline marker."""

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

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


def test_draft_switch_persists_new_runtime_before_marker(monkeypatch, tmp_path):
    with SessionDB(db_path=tmp_path / "state.db") as db:
        monkeypatch.setattr(server, "_get_db", lambda: db)
        monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
        monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
        monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
        monkeypatch.setattr(server, "_session_default_route", lambda _session: ("old-model", "test-provider"))
        monkeypatch.setattr(server, "_restart_slash_worker", lambda *_args: None)
        monkeypatch.setattr(server, "_emit_session_info", lambda *_args: None)
        response = server.handle_request({
            "id": "create", "method": "session.create", "params": {"source": "desktop"}})
        sid = response["result"]["session_id"]
        session = server._sessions[sid]
        key = session["session_key"]
        agent = SimpleNamespace(_session_db=db, session_id=key, model="old-model", provider="test-provider",
                                requested_provider="test-provider", base_url="", api_mode="chat_completions",
                                reasoning_config=None, service_tier=None)

        def switch_model(*, new_model, new_provider, **_kwargs):
            agent.model, agent.provider = new_model, new_provider

        agent.switch_model = switch_model
        session["agent"] = agent
        result = SimpleNamespace(new_model="next-model", target_provider="test-provider", api_key=None,
                                 base_url="", api_mode="chat_completions", runtime_capabilities=None)
        try:
            assert db.get_session(key) is None
            server._commit_agent_switch(sid, session, agent, result, "old-model", None)
            row = db.get_session(key)
            assert row["model"] == "next-model"
            config = json.loads(row["model_config"])
            assert config["model"] == "next-model"
            assert config["provider"] == "test-provider"
            assert len(db.get_messages(key)) == 1
            assert db.get_messages(key)[0]["display_kind"] == "model_switch"
        finally:
            server._sessions.pop(sid, None)


@pytest.fixture
def draft_switch(monkeypatch, tmp_path):
    with SessionDB(db_path=tmp_path / "state.db") as db:
        monkeypatch.setattr(server, "_get_db", lambda: db)
        for name in ("_schedule_agent_build", "_schedule_session_cap_enforcement", "_register_session_cwd"):
            monkeypatch.setattr(server, name, lambda *_args: None)
        monkeypatch.setattr(server, "_session_default_route", lambda _session: ("old-model", "test-provider"))
        monkeypatch.setattr(server, "_config_model_target", lambda: ("profile-model", "test-provider"))
        restart, announce, persist_global = Mock(), Mock(), Mock()
        monkeypatch.setattr(server, "_restart_slash_worker", restart)
        monkeypatch.setattr(server, "_emit_session_info", announce)
        monkeypatch.setattr("hermes_cli.model_switch.persist_model_selection", persist_global)
        result = SimpleNamespace(success=True, new_model="next-model", target_provider="test-provider",
                                 api_key="", base_url="", api_mode="chat_completions", runtime_capabilities=None,
                                 warning_message="", model_info=None)
        monkeypatch.setattr("hermes_cli.model_switch.switch_model", lambda **_kwargs: result)
        response = server.handle_request({
            "id": "create", "method": "session.create", "params": {"source": "desktop"}})
        assert "result" in response, response
        sid = response["result"]["session_id"]
        session = server._sessions[sid]
        agent = SimpleNamespace(_session_db=db, session_id=session["session_key"], model="old-model",
                                provider="test-provider", requested_provider="test-provider", base_url="",
                                api_key="", api_mode="chat_completions", reasoning_config=None, service_tier=None)

        def switch_model(*, new_model, new_provider, **_kwargs):
            agent.model, agent.provider = new_model, new_provider

        agent.switch_model = Mock(side_effect=switch_model)
        session.update(agent=agent, follow_profile_config=True,
                       composer_override_profile={"model": "previous-profile-model", "provider": "test-provider"},
                       model_override={"model": "old-model", "provider": "test-provider"})
        try:
            assert db.get_session(agent.session_id) is None
            yield SimpleNamespace(db=db, sid=sid, session=session, agent=agent,
                                  restart=restart, announce=announce, persist_global=persist_global)
        finally:
            server._sessions.pop(sid, None)


def _assert_switch_refused(draft, history_version):
    assert draft.agent.model == "old-model"
    draft.agent.switch_model.assert_not_called()
    draft.restart.assert_not_called()
    draft.announce.assert_not_called()
    draft.persist_global.assert_not_called()
    assert draft.session["history"] == []
    assert draft.session.get("history_version", 0) == history_version
    assert draft.session["model_override"] == {"model": "old-model", "provider": "test-provider"}
    assert draft.session["composer_override_profile"] == {"model": "previous-profile-model", "provider": "test-provider"}
    assert "one_turn_model_restore" not in draft.session
    assert draft.db.get_session(draft.agent.session_id) is None


def _assert_switch_retry_succeeds(draft, raw):
    reply = server._apply_model_switch(draft.sid, draft.session, raw, confirm_expensive_model=True, count_switch=False)
    assert reply["value"] == draft.agent.model == "next-model"
    draft.agent.switch_model.assert_called_once()
    draft.restart.assert_called_once()
    draft.announce.assert_called_once()
    row = draft.db.get_session(draft.agent.session_id)
    assert row["model"] == json.loads(row["model_config"])["model"] == "next-model"
    rows = draft.db.get_messages(draft.agent.session_id)
    assert len(rows) == 1
    assert rows[0]["display_kind"] == "model_switch"


@pytest.mark.parametrize("scope", ["--session", "--once"])
def test_draft_row_disk_full_refuses_switch_without_mutation_and_can_retry(monkeypatch, draft_switch, scope):
    draft = draft_switch
    raw = f"next-model --provider test-provider {scope}"
    before_version = draft.session.get("history_version", 0)
    # Inject at the real store seam, not the row helper: its disk-full classifier must re-raise.
    with monkeypatch.context() as failure:
        failure.setattr(draft.db, "create_session", Mock(side_effect=sqlite3.OperationalError("database or disk is full")))
        with pytest.raises(sqlite3.OperationalError, match="database or disk is full"):
            server._apply_model_switch(draft.sid, draft.session, raw, confirm_expensive_model=True, count_switch=False)
        _assert_switch_refused(draft, before_version)
    _assert_switch_retry_succeeds(draft, raw)
    if scope == "--once":
        assert draft.session["one_turn_model_restore"]["model"] == "old-model"


def test_unavailable_draft_store_refuses_switch_without_mutation_and_can_retry(monkeypatch, draft_switch):
    draft = draft_switch
    raw = "next-model --provider test-provider --session"
    before_version = draft.session.get("history_version", 0)
    with monkeypatch.context() as failure:
        failure.setattr(server, "_get_db", lambda: None)
        failure.setattr(server, "_db_error", "synthetic store open failure")
        with pytest.raises(ValueError, match="session database is unavailable"):
            server._apply_model_switch(draft.sid, draft.session, raw, confirm_expensive_model=True, count_switch=False)
        _assert_switch_refused(draft, before_version)
    _assert_switch_retry_succeeds(draft, raw)
