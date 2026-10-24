"""Mobile enrollment failures are classified without retaining request content."""
import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_mobile_session_parity import ordinary
from tests.tui_gateway.test_methods_mobile_push import push_service
from tests.tui_gateway.test_methods_mobile import mobile_home, peer, rpc
from tui_gateway import server


@pytest.mark.parametrize("exception", [ValueError, FileNotFoundError, TypeError])
def test_registration_refusal_logs_only_bounded_reason(push_service, ordinary, monkeypatch, caplog, exception):
    def unavailable(params):
        raise exception("PRIVATE_SCOPE_TOKEN_SENTINEL" * 100)

    monkeypatch.setattr(server, "_mobile_session_destination", unavailable)
    with caplog.at_level("WARNING"):
        response = rpc("mobile.session_push.register", **ordinary)
    assert response["error"]["code"] == 4400
    reasons = [r.message for r in caplog.records if "mobile_notification_refused" in r.message]
    assert reasons, "registration scope failures need a content-free reason"
    assert reasons == [f"mobile_notification_refused method=mobile.session_push.register reason={exception.__name__}"]
    assert "PRIVATE_SCOPE_TOKEN_SENTINEL" not in caplog.text
    assert ordinary["stored_session_id"] not in reasons[0]
    assert ordinary["device_token"] not in reasons[0]


@pytest.mark.parametrize("method", ["mobile.session_push.register", "mobile.widget.register"])
def test_known_live_chat_defers_enrollment_until_durable_row_exists(push_service, ordinary, mobile_home, peer, monkeypatch, caplog, method):
    home = mobile_home / "profiles" / "ops"
    key = "fresh-chat"
    monkeypatch.setitem(server._sessions, "fresh-runtime", {
        "session_key": key, "profile_home": str(home), "transport": peer,
    })
    params = {**ordinary, "stored_session_id": key}
    if method == "mobile.widget.register":
        params.pop("device_token")
        params.update(read_token="12" * 32, widget_token="")
    with caplog.at_level("WARNING"):
        pending = rpc(method, **params)
    assert pending["error"]["code"] == 4401, "known pre-persistence chat is temporary, not an invalid scope"
    assert any("reason=durable_session_pending" in r.message for r in caplog.records)
    with SessionDB(db_path=home / "state.db") as db:
        assert db.get_session(key) is None, "enrollment must not materialize an empty chat"
        db.create_session(key, source="tui")
    registered = rpc(method, **params)
    assert registered["result"]["resolved_session_id"] == key
    assert registered["result"]["destination"]["session_id"] == key


def test_missing_foreign_live_chat_is_still_refused(push_service, ordinary, mobile_home, monkeypatch):
    monkeypatch.setitem(server._sessions, "foreign-runtime", {
        "session_key": "foreign-chat", "profile_home": str(mobile_home / "profiles" / "ops"),
        "transport": object(),
    })
    response = rpc("mobile.session_push.register", **{**ordinary, "stored_session_id": "foreign-chat"})
    assert response["error"]["code"] == 4400
