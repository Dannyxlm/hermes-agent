"""Pay presentation follows U6's canonical primary, never the triggering event."""
import json

import pytest

from tests.tui_gateway.test_methods_mobile import mobile_home as _mobile_home, peer as _peer, rpc
from tests.tui_gateway.test_methods_mobile_push import push_service as _push_service
from tests.tui_gateway.test_human_input_push import enrolled as _enrolled, ask, pending_jobs
from tui_gateway import server, server_requests
from tui_gateway.mobile_push_provider import DeliveryResult

mobile_home, peer, push_service, enrolled = _mobile_home, _peer, _push_service, _enrolled


def payment(host="first.test", total="$12.34"):
    return {"request_id": f"queue-{host}", "surface": "vault-payment", "kind": "payment",
            "choices": ["once", "deny"], "command": "PRIVATE_COMMAND",
            "payment": {"v": 1, "origin": f"https://{host}", "origin_bound": False,
                        "total": {"amount_minor": 1234, "currency": "USD", "display": total,
                                  "source": "page", "label": "Total"},
                        "lines": [{"name": "item", "qty": 1, "amount_minor": 1234}],
                        "card": {"label": "Fixture card", "last4": "1234"},
                        "checks": {"total_found": True, "over_limit": True}}}


def payload(service):
    return json.loads(pending_jobs(service)[-1]["payload"])


def test_payment_copy_and_primary_move_together(enrolled, push_service):
    sid, _, _ = enrolled
    first = ask(sid, "approval", payment())
    second = ask(sid, "approval", payment("second.test", "$56.78"))
    before = payload(push_service)
    assert before["aps"]["alert"]["body"] == "Approve $12.34 at first.test? Fixture card ••1234 · 1 item"
    target = before["hermex.destination"]["human_input"]
    assert target["request_id"] == first["id"]
    assert set(target["allowed_actions"]) == {"open", "deny", "suggest"}
    assert "payment" not in target and "PRIVATE_COMMAND" not in json.dumps(before)
    assert server_requests.resolve_response({"id": first["id"], "result": {"choice": "deny"}})
    after = payload(push_service)
    assert "second.test" in after["aps"]["alert"]["body"]
    assert after["hermex.destination"]["human_input"]["request_id"] == second["id"]
    server_requests.cancel(sid)
    server._mobile_push_capture(first)
    assert pending_jobs(push_service) == []


@pytest.mark.parametrize("method,params,expected", [
    ("clarify", {"questions": [{"qid": "q", "question": "Earlier question?"}]}, "Earlier question?"),
    ("vault.code", {}, None),
])
def test_payment_cannot_replace_earlier_question_or_secure_copy(enrolled, push_service, method, params, expected):
    sid, _, _ = enrolled
    first = ask(sid, method, params)
    ask(sid, "approval", payment())
    result = payload(push_service)
    assert result["hermex.destination"]["human_input"]["request_id"] == first["id"]
    assert "first.test" not in json.dumps(result) and "1234" not in json.dumps(result)
    if expected:
        assert result["aps"]["alert"]["body"] == expected
    else:
        assert result["aps"]["category"] == "HERMEX_SECURE_INPUT_V1"


def test_payment_preview_revocation_applies_to_delivery_and_retry(enrolled, push_service):
    sid, _, ids = enrolled
    ask(sid, "approval", payment())
    refresh = {k: ids[k] for k in ("installation_id", "connection_id", "device_token", "environment")}
    assert rpc("mobile.push.refresh", **refresh, preview_enabled=False)["result"]["updated"] == 1
    job = push_service.store.claim()
    assert job is not None
    assert "first.test" not in json.dumps(job["payload"])
    assert "Fixture card" not in json.dumps(job["payload"])
    push_service.store.finish(job, DeliveryResult("retry", "fixture"))
    assert rpc("mobile.push.unregister", **{k: ids[k] for k in ("installation_id", "connection_id")})["result"]["removed"] == 1
    assert not pending_jobs(push_service)


def test_preview_refuses_mismatched_canonical_revision(enrolled):
    from tui_gateway.mobile_push_attention import descriptor, input_preview
    sid, _, _ = enrolled
    frame = ask(sid, "approval", payment())
    target = descriptor(frame)
    target["request_revision"] += 1
    assert input_preview(server._sessions[sid], sid, {"primary": target}) == ""
