"""Native RPC contracts reject cross-surface params and retain shipped optional fields."""
import pytest
from pydantic import ValidationError

from tui_gateway.contracts import METHODS
from tui_gateway.contracts.registry import validate_params

SCOPE = {"profile": "ava", "canonical_root_id": "root", "session_id": "live"}
REGISTRATION = {"installation_id": "installation", "connection_id": "connection", "environment": "production"}


@pytest.mark.parametrize("name", [name for name in METHODS if name.startswith("mobile.")])
def test_mobile_methods_reject_undeclared_parameters(name):
    _, error = validate_params(METHODS[name], {"unrecognized_mobile_parameter": True})
    assert error is not None
    assert "unrecognized_mobile_parameter" in error


@pytest.mark.parametrize("name,params", [
    ("mobile.capabilities", {}),
    ("mobile.bots", {"offset": 0, "limit": 20}),
    ("mobile.open", {"profile": "ava", "canonical_root_id": "root", "limit": 20, "before_row_id": 42}),
    ("mobile.snapshot", {**SCOPE, "before_row_id": 42}),
    ("mobile.submit", {**SCOPE, "text": "Hello"}),
    ("mobile.stop", SCOPE),
    ("mobile.approval.respond", {**SCOPE, "request_id": "srq-approval", "choice": "deny"}),
    ("mobile.clarify.respond", {**SCOPE, "request_id": "srq-clarify", "answer": "Blue", "question_id": "color"}),
    ("mobile.push.status", {}),
    ("mobile.push.register", {**SCOPE, **REGISTRATION, "device_token": "token", "categories": ["attention"], "preview_enabled": True}),
    ("mobile.push.refresh", {**REGISTRATION, "device_token": "token"}),
    ("mobile.push.unregister", {"installation_id": "installation", "connection_id": "connection"}),
    ("mobile.activity.register", {**SCOPE, **REGISTRATION, "activity_token": "token", "activity_id": "activity", "run_id": "run"}),
    ("mobile.activity.refresh", {**REGISTRATION, "activity_token": "token", "activity_id": "activity", "run_id": "run"}),
    ("mobile.activity.unregister", {"installation_id": "installation", "connection_id": "connection", "subscription_id": "subscription"}),
])
def test_mobile_params_accept_shipped_fields(name, params):
    assert METHODS[name].params.model_validate(params)


def test_mobile_scope_and_activity_subscription_are_required():
    with pytest.raises(ValidationError):
        METHODS["mobile.submit"].params.model_validate({"text": "Hello", "session_id": "live"})
    with pytest.raises(ValidationError):
        METHODS["mobile.activity.unregister"].params.model_validate({"installation_id": "i", "connection_id": "c"})


def test_mobile_clarify_result_retains_batch_and_expiry_semantics():
    model = METHODS["mobile.clarify.respond"].result
    assert model.model_validate({"status": "ok", "remaining": ["second"]}).remaining == ["second"]
    assert model.model_validate({"status": "expired"}).status == "expired"
    with pytest.raises(ValidationError):
        model.model_validate({"status": "queued"})
