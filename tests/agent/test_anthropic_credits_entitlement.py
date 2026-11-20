"""Anthropic 429 credits_required classification and model-scoped entitlement benching."""
from __future__ import annotations

import time
from types import SimpleNamespace

from agent.credential_pool import CredentialPool, PooledCredential
from agent.credential_pool_model_cooldowns import MODEL_ENTITLEMENT_BENCH_SECONDS, model_cooldown_until
from agent.error_classifier import FailoverReason, classify_api_error


class _Anthropic429(Exception):
    def __init__(self, body: dict, *, retry_after: str | None = None):
        super().__init__(f"Error code: 429 - {body}")
        self.status_code = 429
        self.body = body
        headers = {"retry-after": retry_after} if retry_after is not None else {}
        self.response = SimpleNamespace(status_code=429, headers=headers, json=lambda: body)


def test_unentitled_model_credits_required_classifies_as_entitlement_and_benches_model_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    exc = _Anthropic429({
        "type": "error",
        "error": {
            "type": "rate_limit_error",
            "message": "Usage credits are required for this model.",
            "details": {
                "error_code": "credits_required",
                "disabled_reason": "org_level_disabled",
                "exhausted_included_allowance": False,
                "model": "claude-fable-5-2",
            },
        },
    }, retry_after="930000")
    classified = classify_api_error(exc, provider="anthropic", model="claude-fable-5-2")
    assert classified.reason == FailoverReason.model_entitlement
    assert classified.retryable is False
    assert classified.should_rotate_credential is True
    assert classified.should_fallback is True

    entries = [
        PooledCredential(provider="anthropic", id="pro1", label="Pro", priority=0,
                         auth_type="oauth", source="manual:hermes_pkce", access_token="fixture-pro"),
        PooledCredential(provider="anthropic", id="max1", label="Max", priority=1,
                         auth_type="oauth", source="manual:hermes_pkce", access_token="fixture-max"),
    ]
    pool = CredentialPool("anthropic", entries)
    nxt = pool.mark_exhausted_and_rotate(
        status_code=400, credential_id="pro1", api_key_hint="fixture-pro",
        failure_reason=classified.reason.value, model="claude-fable-5-2",
    )
    assert nxt is not None and nxt.id == "max1"
    pro = next(e for e in pool.entries() if e.id == "pro1")
    until = model_cooldown_until(pro, "claude-fable-5-2")
    assert until is not None
    assert until - time.time() > MODEL_ENTITLEMENT_BENCH_SECONDS - 60
    # Sibling models remain eligible on the Pro credential.
    assert model_cooldown_until(pro, "claude-opus-5-5") is None


def test_exhausted_included_allowance_credits_required_stays_rate_limit():
    exc = _Anthropic429({
        "type": "error",
        "error": {
            "type": "rate_limit_error",
            "message": "Usage credits are required for this model.",
            "details": {
                "error_code": "credits_required",
                "disabled_reason": "out_of_credits",
                "exhausted_included_allowance": True,
                "model": "claude-fable-5-2",
            },
        },
    }, retry_after="3600")
    classified = classify_api_error(exc, provider="anthropic", model="claude-fable-5-2")
    assert classified.reason == FailoverReason.rate_limit
    assert classified.retryable is True


def test_fast_mode_credits_required_without_details_model_stays_rate_limit():
    exc = _Anthropic429({
        "type": "error",
        "error": {
            "type": "rate_limit_error",
            "message": "Usage credits are required for fast mode.",
            "details": {
                "error_code": "credits_required",
                "exhausted_included_allowance": False,
            },
        },
    }, retry_after="0")
    classified = classify_api_error(exc, provider="anthropic", model="claude-opus-5-5")
    assert classified.reason == FailoverReason.rate_limit
