"""Payment approval contract (Hermex Pay dock): what a vault-payment approval carries, and what it never does.

The user approves one total at one exact origin with one card. The origin comes from the browser, the card facts
from the vault, the money from a deterministic page read; the model's own words never set any of them.
"""

import json
from unittest.mock import patch

import pytest

import tools.approval as approval_module
from tools import approval_context
from tools.approval import clear_session
from tools.approval_context import set_current_session_key

_CARD = {"card_number": "4111111111114821", "exp_month": "07", "exp_year": "2029", "cvc": "321",
         "cardholder_name": "Test Holder"}
_CONTROLS = [{"autocomplete": "cc-number", "index": 0, "type": "text"},
             {"autocomplete": "cc-exp", "index": 1, "type": "text"},
             {"autocomplete": "cc-csc", "index": 2, "type": "text"}]
_SUMMARY = {"site_name": "Harbor Type Foundry", "app_name": "", "title": "Checkout",
            "currency_hint": "",
            "rows": ["Plex Serif — Desktop licence ×1 $29.00", "Tax $3.77", "Order total USD $32.77",
                     # Page text naming another site must never become the approved origin.
                     "Pay securely at https://evil.example"]}


def _meta(**kw):
    from agent.vault_store import VaultItemMeta
    return VaultItemMeta(id="op:crd", kind="payment", label="Expense Card", origin=None, created_at="",
                         any_origin=True, last4=kw.get("last4", "4821"))


class _Backend:
    name, display_name, needs_unlock = "onepassword", "1Password", True

    def __init__(self, meta, limit_text=None):
        self.meta, self.limit_text, self.resolved = meta, limit_text, 0

    def is_unlocked(self):
        return True

    def get_meta(self, handle):
        return self.meta

    def card_last4(self, handle):
        return self.meta.last4

    def card_limit_text(self, handle):
        return self.limit_text

    def resolve_secret(self, handle):
        self.resolved += 1
        return dict(_CARD)


def _run(consent, *, summary=_SUMMARY, backend=None, limits=None, href="https://checkout.harbortype.com/pay"):
    """Drive browser_vault_fill with a fake page. ``consent(message, description, **kw)`` plays the human."""
    from tools import browser_vault_tool
    from agent import redact

    backend = backend or _Backend(_meta())
    secret_exprs = []

    def fake_eval(task_id, expression):
        if "location.href" in expression:
            return {"success": True, "result": href}
        if "__hermesOrderSummary" in expression:
            return {"success": True, "result": json.dumps(summary)}
        return {"success": True, "result": json.dumps(_CONTROLS)}

    def fake_eval_secret(task_id, expression):
        secret_exprs.append(expression)
        return {"success": True, "result": json.dumps({"filled": 3})}

    try:
        with patch("agent.vault_backends.backend_for_handle", return_value=backend), \
             patch.object(browser_vault_tool, "_focus_bound_origin", return_value=None), \
             patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
             patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fake_eval_secret), \
             patch.object(browser_vault_tool, "_payment_limits_config", return_value=limits or {}), \
             patch("tools.approval_prompt.request_elicitation_consent", side_effect=consent):
            raw = browser_vault_tool.browser_vault_fill("op:crd")
    finally:
        redact.clear_vault_redaction_values()
    return raw, secret_exprs, backend


class _Human:
    def __init__(self, verdict="accept", auth="face_id"):
        self.verdict, self.auth, self.calls = verdict, auth, []

    def __call__(self, message, description, **kw):
        self.calls.append({"message": message, "description": description, **kw})
        if kw.get("details") is not None and self.verdict == "accept":
            kw["details"].update(auth=self.auth, decided_at=1791872700.0)
        return self.verdict


class TestPaymentApprovalContract:
    def test_payload_carries_trusted_origin_card_and_page_sourced_money(self):
        human = _Human()
        raw, secret_exprs, _ = _run(human, limits={"Expense Card": {"amount": 50, "currency": "USD",
                                                                    "period": "per_purchase"}})
        call = human.calls[0]
        extra = call["extra"]
        assert extra["surface"] == "vault-payment"
        assert extra["allow_session"] is False and extra["allow_permanent"] is False
        assert extra["expires_at"] > extra["created_at"] and extra["timeout_seconds"] > 0
        payment = extra["payment"]
        assert payment["v"] == 1
        assert payment["origin"] == "https://checkout.harbortype.com"  # from the browser, not page text
        assert payment["origin_bound"] is False
        assert payment["merchant"] == {"name": "Harbor Type Foundry", "source": "page"}
        assert payment["total"]["amount_minor"] == 3277 and payment["total"]["currency"] == "USD"
        assert payment["total"]["source"] == "page"
        assert payment["lines"] == [{"name": "Plex Serif — Desktop licence", "qty": 1, "amount_minor": 2900}]
        assert payment["adjustments"] == [{"kind": "tax", "amount_minor": 377}]
        assert payment["card"] == {"label": "Expense Card", "last4": "4821", "backend": "onepassword",
                                   "limit": {"amount_minor": 5000, "currency": "USD", "period": "per_purchase",
                                             "source": "config"}}
        assert payment["checks"] == {"total_found": True, "over_limit": False}
        # Legacy surfaces still read command/description; amount + origin keep two payments from coalescing.
        assert call["message"] == "Pay $32.77 at https://checkout.harbortype.com with Expense Card ••4821"
        assert call["description"].startswith("Approve $32.77 at checkout.harbortype.com?")
        assert call["title"] == "Approve $32.77 at checkout.harbortype.com?"
        assert "evil.example" not in json.dumps(extra)
        out = json.loads(raw)
        assert out["success"] is True and len(secret_exprs) == 1

    def test_the_model_cannot_choose_the_number(self):
        """The handler takes only a handle: an injected amount argument never reaches the approval."""
        from tools import browser_vault_tool
        human = _Human()
        schema_props = browser_vault_tool.BROWSER_VAULT_FILL_SCHEMA["parameters"]["properties"]
        assert set(schema_props) == {"handle"}
        _run(human)
        assert human.calls[0]["extra"]["payment"]["total"]["display"] == "$32.77"

    def test_total_not_found_still_asks_and_says_so(self):
        human = _Human()
        summary = {**_SUMMARY, "rows": ["Card details"]}
        raw, secret_exprs, _ = _run(human, summary=summary)
        payment = human.calls[0]["extra"]["payment"]
        assert payment["total"] is None and payment["checks"] == {"total_found": False, "over_limit": None}
        assert payment["lines"] == [] and payment["adjustments"] == []
        assert human.calls[0]["title"] == "Approve card fill at checkout.harbortype.com?"
        assert "Total not found" in human.calls[0]["description"]
        assert json.loads(raw)["success"] is True and len(secret_exprs) == 1

    def test_over_limit_warns_but_does_not_block(self):
        human = _Human()
        raw, secret_exprs, _ = _run(human, limits={"Expense Card": {"amount": 20, "currency": "USD"}})
        payment = human.calls[0]["extra"]["payment"]
        assert payment["checks"]["over_limit"] is True
        assert payment["card"]["limit"]["amount_minor"] == 2000 and payment["card"]["limit"]["period"] == "unknown"
        assert "over the $20.00 card limit" in human.calls[0]["description"].lower()
        assert json.loads(raw)["success"] is True and len(secret_exprs) == 1

    def test_limit_from_the_vault_item_when_config_has_none(self):
        human = _Human()
        _run(human, backend=_Backend(_meta(), limit_text="$50"))
        limit = human.calls[0]["extra"]["payment"]["card"]["limit"]
        assert limit == {"amount_minor": 5000, "currency": None, "period": "unknown", "source": "vault"}

    def test_no_limit_anywhere_is_null_not_invented(self):
        human = _Human()
        _run(human)
        payment = human.calls[0]["extra"]["payment"]
        assert payment["card"]["limit"] is None and payment["checks"]["over_limit"] is None

    def test_declined_fills_nothing_and_echoes_a_declined_receipt(self):
        human = _Human("decline")
        raw, secret_exprs, backend = _run(human)
        out = json.loads(raw)
        assert out["error_type"] == "payment_declined" and secret_exprs == [] and backend.resolved == 0
        assert out["payment"]["decision"] == "declined" and out["payment"]["filled"] is False
        assert out["payment"]["origin"] == "https://checkout.harbortype.com"

    def test_expired_is_not_declined(self):
        human = _Human("cancel")
        raw, secret_exprs, backend = _run(human)
        out = json.loads(raw)
        assert out["error_type"] == "payment_expired" and secret_exprs == [] and backend.resolved == 0
        assert out["payment"]["decision"] == "expired"

    def test_approved_receipt_survives_in_the_tool_result(self):
        raw, _, _ = _run(_Human("accept", auth="face_id"))
        out = json.loads(raw)
        receipt = out["payment"]
        assert receipt["decision"] == "approved" and receipt["auth"] == "face_id"
        assert receipt["decided_at"] == 1791872700.0 and receipt["filled"] is True
        assert receipt["total"]["display"] == "$32.77" and receipt["card"]["last4"] == "4821"
        assert receipt["merchant"]["name"] == "Harbor Type Foundry"
        assert receipt["lines"][0]["name"] == "Plex Serif — Desktop licence"

    def test_last4_only_never_the_card_number(self):
        human = _Human()
        raw, _, _ = _run(human)
        for blob in (json.dumps(human.calls[0]), raw):
            assert _CARD["card_number"] not in blob and _CARD["cvc"] not in blob
            assert "07/29" not in blob and "2029" not in blob
            assert "411111" not in blob

    def test_insecure_page_is_refused_before_asking(self):
        human = _Human()
        raw, secret_exprs, _ = _run(human, href="http://checkout.harbortype.com/pay")
        assert json.loads(raw)["error_type"] == "insecure_origin" and human.calls == [] and secret_exprs == []


class TestOnePasswordLast4:
    def test_last4_from_masked_list_metadata(self):
        from agent.vault_backends.onepassword import card_last4_from_list
        assert card_last4_from_list("4111 **** 4821") == "4821"
        assert card_last4_from_list("•••• 4821") == "4821"
        assert card_last4_from_list("4111111111114821") is None  # an unmasked number is never parsed
        assert card_last4_from_list("") is None


@pytest.fixture
def gateway_session(monkeypatch, tmp_path):
    key = "test:session:paydock"
    token = set_current_session_key(key)
    monkeypatch.setenv("HERMES_SESSION_KEY", key)
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: {"mode": "manual", "timeout": 60})
    try:
        yield key
    finally:
        approval_module.unregister_gateway_notify(key)
        try:
            approval_context._approval_session_key.reset(token)
        except Exception:
            pass
        clear_session(key)


def _payment_consent(**kw):
    from tools.approval_prompt import request_elicitation_consent
    return request_elicitation_consent(
        "Pay $32.77 at https://checkout.harbortype.com with Expense Card ••4821", "Approve $32.77?",
        surface="vault-payment", title="Approve $32.77 at checkout.harbortype.com?",
        extra={"surface": "vault-payment", "allow_session": False, "allow_permanent": False,
               "payment": {"v": 1, "origin": "https://checkout.harbortype.com"}}, **kw)


class TestServerRefusesScopes:
    @pytest.mark.parametrize("choice", ["session", "always"])
    def test_scope_answers_never_approve_a_payment(self, gateway_session, choice):
        seen = []

        def notify(data):
            seen.append(dict(data))
            assert approval_module.resolve_gateway_approval(gateway_session, choice, request_id=data["request_id"]) == 0
            assert approval_module.resolve_gateway_approval(gateway_session, "deny", request_id=data["request_id"]) == 1

        approval_module.register_gateway_notify(gateway_session, notify)
        assert _payment_consent() == "decline"
        assert seen[0]["payment"]["origin"] == "https://checkout.harbortype.com"
        assert seen[0]["surface"] == "vault-payment" and seen[0]["allow_session"] is False

    def test_once_approves_and_reports_auth(self, gateway_session):
        approval_module.register_gateway_notify(
            gateway_session, lambda d: approval_module.resolve_gateway_approval(gateway_session, "once",
                                                                               auth="face_id"))
        details = {}
        assert _payment_consent(details=details) == "accept"
        assert details["auth"] == "face_id" and details["decided_at"] > 0

    def test_unknown_auth_label_is_dropped(self, gateway_session):
        approval_module.register_gateway_notify(
            gateway_session, lambda d: approval_module.resolve_gateway_approval(gateway_session, "once",
                                                                               auth="<script>"))
        details = {}
        assert _payment_consent(details=details) == "accept"
        assert details["auth"] is None

    def test_offered_choices_are_once_and_deny_only(self):
        from tui_gateway.server import _approval_request_payload
        payload = _approval_request_payload({"command": "Pay $1.00 at https://a.test with X ••1111",
                                             "surface": "vault-payment", "allow_session": False,
                                             "allow_permanent": False, "payment": {"v": 1, "origin": "https://a.test"}})
        assert payload["choices"] == ["once", "deny"]
        assert payload["payment"] == {"v": 1, "origin": "https://a.test"}  # the redactor leaves the non-secret block alone

    def test_timeout_is_cancel_not_decline(self, gateway_session, monkeypatch):
        monkeypatch.setattr(approval_context, "_get_approval_config", lambda: {"mode": "manual", "timeout": 0})
        approval_module.register_gateway_notify(gateway_session, lambda d: None)
        assert _payment_consent() == "cancel"


class TestSrqResponseCarriesAuth:
    def test_on_result_passes_a_known_auth_label(self, monkeypatch):
        from tui_gateway import server
        captured = {}

        def fake_send_async(method, sid, payload, on_result, **timing):
            assert "timeout" in timing and "deadline_monotonic" in timing
            captured["on_result"] = on_result
            return lambda reason: None

        monkeypatch.setattr("tui_gateway.server_requests.send_async", fake_send_async)
        monkeypatch.setattr(approval_module, "register_gateway_settle", lambda *a, **k: True)
        resolved = {}
        monkeypatch.setattr(approval_module, "resolve_gateway_approval",
                            lambda key, choice, **kw: resolved.update(choice=choice, **kw) or 1)
        server._emit_approval_request("sid-x", {"command": "Pay", "request_id": "r1", "allow_session": False})
        captured["on_result"]({"choice": "once", "auth": "face_id"})
        assert resolved["choice"] == "once" and resolved["auth"] == "face_id" and resolved["request_id"] == "r1"
        captured["on_result"]({"choice": "once", "auth": "rubbish"})
        assert resolved["auth"] is None


class TestPaymentPushCopy:
    @staticmethod
    def preview(monkeypatch, payload):
        from tui_gateway import mobile_push_attention as attention
        frame = {"id": "srq-payment", "method": "approval", "params": payload}
        monkeypatch.setattr(attention, "request_frames", lambda session, sid: {frame["id"]: frame})
        return attention.input_preview({}, "sid", {"primary": attention.descriptor(frame)})

    def test_push_names_total_site_and_card(self, monkeypatch):
        payload = {"surface": "vault-payment", "description": "generic",
                   "payment": {"origin": "https://checkout.harbortype.com",
                               "total": {"display": "$32.77"}, "lines": [{"name": "a"}, {"name": "b"}],
                               "card": {"label": "Expense Card", "last4": "4821"}}}
        assert self.preview(monkeypatch, payload) == \
            "Approve $32.77 at checkout.harbortype.com? Expense Card ••4821 · 2 items"

    def test_push_without_total(self, monkeypatch):
        payload = {"surface": "vault-payment", "payment": {"origin": "https://shop.test", "total": None,
                                                           "lines": [], "card": {"label": "X", "last4": None}}}
        assert self.preview(monkeypatch, payload) == "Approve card fill at shop.test? X"

    def test_ordinary_approval_command_stays_private(self, monkeypatch):
        assert self.preview(monkeypatch, {"description": "run rm"}) == ""
