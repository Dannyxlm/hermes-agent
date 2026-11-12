"""Standalone delivery errors must remain structured and secret-safe without raw tracebacks."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram import standalone as sender
from tests.tools.telegram_stub_errors import TelegramError, install_error_module


@pytest.fixture
def delivery(monkeypatch):
    install_error_module(monkeypatch)
    monkeypatch.setenv("HERMES_TELEGRAM_NOTIFICATIONS", "important")
    monkeypatch.setattr(sender, "_telegram_format", lambda message: (message, "HTML", True))
    monkeypatch.setattr(sender, "_telegram_bot", lambda *_: object())
    monkeypatch.setattr(sender, "_telegram_album_paths", lambda *_: None)


def _failure_text():
    token = "123456789:" + "A" * 35
    query = "standalone-query-canary"
    return f"https://api.telegram.org/bot{token}/sendPhoto?access_token={query}", token, query


def _assert_safe(result, caplog, token, query):
    rendered = json.dumps(result) + caplog.text
    assert token not in rendered
    assert query not in rendered
    assert "***" in rendered
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("error_class", [TelegramError, OSError, ValueError])
def test_unexpected_setup_error_is_a_sanitized_result(delivery, monkeypatch, caplog, error_class):
    text, token, query = _failure_text()

    def fail(*_):
        raise error_class(text)

    monkeypatch.setattr(sender, "_telegram_bot", fail)
    result = asyncio.run(sender._send_telegram("synthetic", "123", "hello"))
    assert "Telegram send failed" in result["error"]
    assert not result.get("success")
    _assert_safe(result, caplog, token, query)


@pytest.mark.parametrize("error_class", [TelegramError, OSError, ValueError, RuntimeError, TypeError])
def test_operational_media_error_continues_and_redacts(delivery, monkeypatch, caplog, tmp_path, error_class):
    text, token, query = _failure_text()
    first, second = tmp_path / "first.png", tmp_path / "second.png"
    first.write_bytes(b"fixture")
    second.write_bytes(b"fixture")
    send = AsyncMock(side_effect=[error_class(text), SimpleNamespace(message_id=42)])
    monkeypatch.setattr(sender, "_telegram_send_one_media", send)
    send_text = AsyncMock(return_value=SimpleNamespace(message_id=41))
    monkeypatch.setattr(sender, "_telegram_send_text_chunk", send_text)
    result = asyncio.run(sender._send_telegram(
        "synthetic", "123", "already sent", media_files=[(str(first), False), (str(second), False)]))
    send_text.assert_awaited_once()
    assert result["success"] and result["message_id"] == "42"
    assert send.await_count == 2 and len(result["warnings"]) == 1
    _assert_safe(result, caplog, token, query)


@pytest.mark.parametrize("error_class", [TelegramError, RuntimeError])
def test_missing_media_caption_error_is_sanitized(delivery, monkeypatch, caplog, tmp_path, error_class):
    text, token, query = _failure_text()
    send = AsyncMock(side_effect=error_class(text))
    monkeypatch.setattr(sender, "_send_telegram_message_with_retry", send)
    result = asyncio.run(sender._send_telegram(
        "synthetic", "123", "caption", media_files=[(str(tmp_path / "missing.png"), False)]))
    assert result["error"] == sender._NO_DELIVERABLE
    send.assert_awaited_once()
    _assert_safe(result, caplog, token, query)


def test_error_boundary_forces_redaction_and_logs_only_class(delivery, monkeypatch, caplog):
    monkeypatch.setenv("HERMES_REDACT_SECRETS", "false")
    monkeypatch.setattr("agent.redact._redact_enabled", lambda: False)
    text, token, query = _failure_text()

    def fail(*_):
        raise RuntimeError(text)

    monkeypatch.setattr(sender, "_telegram_bot", fail)
    result = asyncio.run(sender._send_telegram("synthetic", "123", "hello"))
    _assert_safe(result, caplog, token, query)
    assert [(record.levelname, record.getMessage()) for record in caplog.records] == [
        ("WARNING", "Telegram send failed: RuntimeError")]
