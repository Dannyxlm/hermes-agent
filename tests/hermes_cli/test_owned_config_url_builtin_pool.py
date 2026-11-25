"""Regression tests: canonical built-in URLs are not hijacked by providers.<alias> API-credit entries."""

from __future__ import annotations

import json
import time


def _write_anthropic_oauth_pool_home(tmp_path, monkeypatch, *, base_url: str = "https://api.anthropic.com"):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_API_CREDITS_KEY", "sk-ant-api03-credit-key-1234567890")
    monkeypatch.setenv("OPENAI_API_CREDITS_KEY", "sk-proj-openai-credit-key-1234567890")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-openai-builtin-key-9876543210")

    cfg = {
        "model": {
            "provider": "anthropic",
            "default": "claude-opus-4-6",
            "base_url": base_url,
        },
        "providers": {
            "claude-api": {
                "name": "Claude API credits",
                "api": "https://api.anthropic.com",
                "transport": "anthropic_messages",
                "key_env": "CLAUDE_API_CREDITS_KEY",
            },
            "openai-api-credits": {
                "name": "OpenAI API credits",
                "api": "https://api.openai.com/v1",
                "key_env": "OPENAI_API_CREDITS_KEY",
            },
        },
    }
    (home / "config.yaml").write_text(json.dumps(cfg), encoding="utf-8")

    future = time.time() + 7200
    auth_store = {
        "version": 1,
        "providers": {},
        "credential_pool": {
            "anthropic": [
                {
                    "id": "oauth-acct-1",
                    "label": "Primary Max",
                    "access_token": "sk-ant-oat01-pool-token-one-1234567890",
                    "refresh_token": "sk-ant-ort01-refresh-one",
                    "expires_at": future,
                    "auth_type": "oauth",
                    "source": "manual",
                    "priority": 0,
                },
                {
                    "id": "oauth-acct-2",
                    "label": "Secondary Max",
                    "access_token": "sk-ant-oat01-pool-token-two-1234567890",
                    "refresh_token": "sk-ant-ort01-refresh-two",
                    "expires_at": future,
                    "auth_type": "oauth",
                    "source": "manual",
                    "priority": 1,
                },
            ]
        },
    }
    (home / "auth.json").write_text(json.dumps(auth_store), encoding="utf-8")
    return home


def test_builtin_anthropic_with_canonical_base_url_keeps_oauth_pool(tmp_path, monkeypatch):
    _write_anthropic_oauth_pool_home(tmp_path, monkeypatch)
    from hermes_cli import runtime_provider as rp

    for requested in (None, "anthropic"):
        runtime = rp.resolve_runtime_provider(requested=requested)
        assert runtime["provider"] == "anthropic"
        assert runtime["api_key"] == "sk-ant-oat01-pool-token-one-1234567890"
        assert runtime.get("credential_pool") is not None
        assert len(runtime["credential_pool"].entries()) == 2


def test_builtin_anthropic_v1_canonical_base_url_keeps_oauth_pool(tmp_path, monkeypatch):
    _write_anthropic_oauth_pool_home(tmp_path, monkeypatch, base_url="https://api.anthropic.com/v1")
    from hermes_cli import runtime_provider as rp

    runtime = rp.resolve_runtime_provider(requested="anthropic")
    assert runtime["provider"] == "anthropic"
    assert runtime["api_key"] == "sk-ant-oat01-pool-token-one-1234567890"
    assert runtime.get("credential_pool") is not None
    assert len(runtime["credential_pool"].entries()) == 2


def test_explicit_custom_claude_api_alias_still_resolves_api_credit_key(tmp_path, monkeypatch):
    _write_anthropic_oauth_pool_home(tmp_path, monkeypatch)
    from hermes_cli import runtime_provider as rp

    runtime = rp.resolve_runtime_provider(requested="custom:claude-api")
    assert runtime["api_key"] == "sk-ant-api03-credit-key-1234567890"
    assert runtime["base_url"].rstrip("/") == "https://api.anthropic.com"


def test_builtin_openai_api_canonical_url_is_not_hijacked_by_credit_alias(tmp_path, monkeypatch):
    home = _write_anthropic_oauth_pool_home(tmp_path, monkeypatch)
    cfg = json.loads((home / "config.yaml").read_text(encoding="utf-8"))
    cfg["model"] = {
        "provider": "openai-api",
        "default": "gpt-5",
        "base_url": "https://api.openai.com/v1",
    }
    (home / "config.yaml").write_text(json.dumps(cfg), encoding="utf-8")

    from hermes_cli import runtime_provider as rp

    runtime = rp.resolve_runtime_provider(requested="openai-api")
    assert runtime["provider"] == "openai-api"
    assert runtime["api_key"] == "sk-proj-openai-builtin-key-9876543210"

    custom_rt = rp.resolve_runtime_provider(requested="custom:openai-api-credits")
    assert custom_rt["api_key"] == "sk-proj-openai-credit-key-1234567890"
