"""Synthetic recovery/cancellation contracts at payment data boundaries."""

import asyncio
import json
from unittest.mock import patch

import pytest

from agent.vault_backends import UnlockRequired
from agent.vault_backends.onepassword import OnePasswordLoginBackend
from agent.vault_store import VaultError
from tests.tools.test_vault_payment_approval import _Backend, _Human, _meta, _run
from tools import browser_vault_tool as vault


@pytest.mark.parametrize("error_type", [VaultError, OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError])
def test_resolution_failure_keeps_approved_receipt_without_values(error_type, caplog):
    marker = "synthetic-private-provider-value"

    class BrokenBackend(_Backend):
        def resolve_secret(self, handle):
            raise error_type(marker)

    human = _Human()
    raw, writes, _ = _run(human, backend=BrokenBackend(_meta()))
    result = json.loads(raw)
    assert result["error_type"] == "secret_resolution_failed"
    assert result["fresh_request_needed"] is True
    assert result["payment"]["decision"] == "approved"
    assert result["payment"]["filled"] is False
    assert not writes and len(human.calls) == 1
    assert marker not in raw + caplog.text


@pytest.mark.parametrize("method", ["card_limit_text", "card_last4"])
@pytest.mark.parametrize("error_type", [RuntimeError, UnlockRequired])
def test_optional_metadata_failure_never_bypasses_human(method, error_type, caplog):
    backend = _Backend(_meta())
    error = UnlockRequired(backend) if error_type is UnlockRequired else RuntimeError("synthetic-private-metadata")
    human = _Human("decline")
    with patch.object(backend, method, side_effect=error):
        raw, writes, _ = _run(human, backend=backend)
    result = json.loads(raw)
    assert result["error_type"] == "payment_declined"
    assert len(human.calls) == 1 and backend.resolved == 0 and not writes
    key = {"card_limit_text": "limit", "card_last4": "last4"}[method]
    assert result["payment"]["card"][key] is None
    assert "synthetic-private-metadata" not in raw + caplog.text


def test_missing_fill_target_keeps_approved_unfilled_receipt():
    with patch("agent.vault_login_classifier.select_checkout_fills", return_value=[]):
        raw, writes, _ = _run(_Human())
    result = json.loads(raw)
    assert result["success"] is False and not writes
    assert result["payment"]["decision"] == "approved" and result["payment"]["filled"] is False


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, TypeError, ValueError])
def test_checkout_read_failure_after_consent_refuses_before_resolution(error_type, caplog):
    human = _Human()

    def consent(*args, **kwargs):
        verdict = human(*args, **kwargs)
        original = vault._eval_js.side_effect

        def evaluate(task_id, expression):
            if "__hermesOrderSummary" in expression:
                raise error_type("synthetic-private-page-value")
            return original(task_id, expression)

        vault._eval_js.side_effect = evaluate
        return verdict

    raw, writes, backend = _run(consent)
    result = json.loads(raw)
    assert result["error_type"] == "payment_changed" and result["fresh_request_needed"] is True
    assert backend.resolved == 0 and not writes
    assert "synthetic-private-page-value" not in raw + caplog.text


@pytest.mark.parametrize("config", [{}, {"vault": []}, {"vault": "bad"}, {"vault": {"payment_limits": []}}])
def test_invalid_limit_config_is_unknown(config):
    with patch("hermes_cli.config.load_config_readonly", return_value=config):
        assert vault._payment_limits_config() == {}


def test_unreadable_limit_config_is_unknown(caplog):
    with patch("hermes_cli.config.load_config_readonly", side_effect=OSError("synthetic-private-path")):
        assert vault._payment_limits_config() == {}
    assert "synthetic-private-path" not in caplog.text


@pytest.mark.parametrize("error_type", [TypeError, ValueError, OverflowError, AttributeError])
def test_bad_timeout_uses_finite_default_without_approving(error_type):
    human = _Human("cancel")
    with patch("tools.approval_context._get_approval_timeout", side_effect=error_type("synthetic-timeout")):
        raw, writes, backend = _run(human)
    assert json.loads(raw)["error_type"] == "payment_expired"
    assert human.calls[0]["extra"]["timeout_seconds"] == 300.0
    assert not writes and backend.resolved == 0


@pytest.mark.parametrize("error_type", [asyncio.CancelledError, KeyboardInterrupt])
@pytest.mark.parametrize("method", ["card_limit_text", "card_last4", "resolve_secret"])
def test_cancellation_propagates_without_injection(method, error_type):
    backend = _Backend(_meta())
    with patch.object(backend, method, side_effect=error_type), \
         patch("agent.vault_login_classifier.build_fill_js") as inject:
        with pytest.raises(error_type):
            _run(_Human(), backend=backend)
        inject.assert_not_called()


@pytest.mark.parametrize("failure", ["missing", "invalid_json", "locked"])
def test_onepassword_limit_lookup_has_value_free_fallback(failure, caplog):
    backend = OnePasswordLoginBackend.__new__(OnePasswordLoginBackend)
    errors = {
        "missing": RuntimeError("synthetic-private-cli-output"),
        "invalid_json": json.JSONDecodeError("synthetic-private-json", "", 0),
        "locked": UnlockRequired(backend),
    }
    with patch.object(backend, "_item_get", side_effect=[errors[failure], "$50 per purchase"]):
        assert backend.card_limit_text("op:synthetic") == "$50 per purchase"
    assert "synthetic-private" not in caplog.text
