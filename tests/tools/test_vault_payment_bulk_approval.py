"""Bulk approvals cannot spend: payment requests remain pending for individual answers."""

import pytest

from tools import approval
from tools.approval_gateway_wait import _ApprovalEntry


@pytest.mark.parametrize("choice", ["once", "session", "always"])
def test_bulk_approval_skips_each_payment_and_preserves_queue_order(monkeypatch, choice):
    key = "synthetic:pay-bulk"
    first = _ApprovalEntry({"surface": "vault-payment", "request_id": "pay-1"})
    ordinary = _ApprovalEntry({"surface": "gateway", "request_id": "shell-1"})
    second = _ApprovalEntry({"surface": "vault-payment", "request_id": "pay-2"})
    monkeypatch.setattr(approval, "_gateway_queues", {key: [first, ordinary, second]})
    assert approval.resolve_gateway_approval(key, choice, resolve_all=True) == 1
    assert ordinary.result == choice and ordinary.event.is_set()
    assert approval.list_gateway_approvals(key) == [first.data, second.data]
    assert first.result is None and second.result is None
    assert not first.event.is_set() and not second.event.is_set()
    assert approval.resolve_gateway_approval(key, "once", request_id="pay-2") == 1
    assert approval.list_gateway_approvals(key) == [first.data]
    assert approval.resolve_gateway_approval(key, "once", resolve_all=True) == 0
    assert approval.resolve_gateway_approval(key, "once", request_id="pay-1") == 1
    assert approval.list_gateway_approvals(key) == []


def test_bulk_denial_still_cancels_payments(monkeypatch):
    key = "synthetic:pay-deny"
    entry = _ApprovalEntry({"surface": "vault-payment", "request_id": "pay-1"})
    monkeypatch.setattr(approval, "_gateway_queues", {key: [entry]})
    assert approval.resolve_gateway_approval(key, "deny", resolve_all=True) == 1
    assert entry.result == "deny" and entry.event.is_set()
    assert approval.list_gateway_approvals(key) == []
