"""Synthetic payment approval regressions: stale carts and secret-resolution failures."""

import json
from copy import deepcopy

import pytest

from tests.tools.test_vault_payment_approval import _Backend, _Human, _SUMMARY, _meta, _run


@pytest.mark.parametrize("change", ["total", "currency", "merchant", "item", "quantity", "adjustment", "missing"])
def test_changed_checkout_needs_fresh_approval_before_secret_resolution(change):
    summary = deepcopy(_SUMMARY)
    human = _Human()

    def consent(*args, **kwargs):
        verdict = human(*args, **kwargs)
        mutations = {
            "total": lambda: summary["rows"].__setitem__(-2, "Order total USD $40.00"),
            "currency": lambda: summary["rows"].__setitem__(-2, "Order total CAD $32.77"),
            "merchant": lambda: summary.update(site_name="Different Merchant"),
            "item": lambda: summary["rows"].__setitem__(0, "Different licence ×1 $29.00"),
            "quantity": lambda: summary["rows"].__setitem__(0, "Plex Serif — Desktop licence ×2 $29.00"),
            "adjustment": lambda: summary.update(rows=[
                "Plex Serif — Desktop licence ×1 $28.00", "Tax $4.77", "Order total USD $32.77"]),
            "missing": summary.clear,
        }
        mutations[change]()
        return verdict

    raw, writes, backend = _run(consent, summary=summary)
    out = json.loads(raw)
    assert out["success"] is False
    assert out["error_type"] == "payment_changed"
    assert out["fresh_request_needed"] is True
    assert out["payment"]["decision"] == "approved" and out["payment"]["filled"] is False
    assert out["payment"]["total"]["amount_minor"] == 3277
    assert backend.resolved == 0 and writes == [] and len(human.calls) == 1


@pytest.mark.parametrize("failure", ["eval_failed", "exception", "invalid", "malformed"])
def test_unreadable_checkout_after_approval_fails_closed(failure):
    from tools import browser_vault_tool

    human = _Human()

    def consent(*args, **kwargs):
        verdict = human(*args, **kwargs)
        evaluate = browser_vault_tool._eval_js.side_effect

        def changed_eval(task_id, expression):
            if "__hermesOrderSummary" not in expression:
                return evaluate(task_id, expression)
            if failure == "exception":
                raise RuntimeError("synthetic-summary-error-do-not-echo")
            if failure == "eval_failed":
                return {"success": False, "error": "synthetic-summary-error-do-not-echo"}
            return {"success": True, "result": [] if failure == "invalid" else {"rows": 7}}

        browser_vault_tool._eval_js.side_effect = changed_eval
        return verdict

    raw, writes, backend = _run(consent)
    out = json.loads(raw)
    assert out["error_type"] == "payment_changed" and out["fresh_request_needed"] is True
    assert out["payment"]["decision"] == "approved" and out["payment"]["filled"] is False
    assert backend.resolved == 0 and writes == [] and len(human.calls) == 1
    assert "synthetic-summary-error-do-not-echo" not in raw


@pytest.mark.parametrize("failure", ["locked", "exception"])
def test_secret_resolution_failure_preserves_safe_approved_receipt(failure):
    from agent.vault_backends import UnlockRequired

    class FailingBackend(_Backend):
        def resolve_secret(self, handle):
            if failure == "locked":
                raise UnlockRequired(self)
            # Exception text can carry provider secrets: it must never be returned or logged.
            raise RuntimeError("synthetic-provider-secret-do-not-echo")

    human = _Human()
    raw, writes, _ = _run(human, backend=FailingBackend(_meta()))
    out = json.loads(raw)
    assert out["success"] is False
    assert out["error_type"] == ("unlock_required" if failure == "locked" else "secret_resolution_failed")
    assert out["fresh_request_needed"] is True
    assert out["payment"]["decision"] == "approved" and out["payment"]["filled"] is False
    assert out["payment"]["auth"] == "face_id" and out["payment"]["decided_at"] == 1791872700.0
    assert "synthetic-provider-secret-do-not-echo" not in raw
    assert writes == [] and len(human.calls) == 1
