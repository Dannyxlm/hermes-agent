"""Mobile enrollment failures are classified without retaining request content."""
import pytest

from tests.tui_gateway.test_mobile_session_parity import ordinary as ordinary  # noqa: PLC0414 - pytest fixture
from tests.tui_gateway.test_methods_mobile_push import push_service as push_service  # noqa: PLC0414 - pytest fixture
from tests.tui_gateway.test_methods_mobile import mobile_home as mobile_home, peer as peer, rpc  # noqa: PLC0414 - pytest fixtures
from tui_gateway import server


@pytest.mark.parametrize("method", ["mobile.session_push.register", "mobile.widget.register"])
@pytest.mark.parametrize("exception", [ValueError, FileNotFoundError, TypeError])
def test_registration_refusal_logs_only_bounded_reason(push_service, ordinary, monkeypatch, caplog, exception, method):
    def unavailable(params):
        raise exception("PRIVATE_SCOPE_TOKEN_SENTINEL" * 100)

    monkeypatch.setattr(server, "_mobile_session_destination", unavailable)
    params = dict(ordinary)
    if method == "mobile.widget.register":
        params.pop("device_token")
        params.update(read_token="12" * 32, widget_token="")
    with caplog.at_level("WARNING"):
        response = rpc(method, **params)
    assert response["error"]["code"] == 4400
    reasons = [r.message for r in caplog.records if "mobile_notification_refused" in r.message]
    assert reasons, "registration scope failures need a content-free reason"
    assert reasons == [f"mobile_notification_refused method={method} reason={exception.__name__}"]
    assert "PRIVATE_SCOPE_TOKEN_SENTINEL" not in caplog.text
    assert ordinary["stored_session_id"] not in reasons[0]
    assert ordinary["device_token"] not in reasons[0]
