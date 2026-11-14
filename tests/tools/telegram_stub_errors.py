"""Minimal SDK error surface for the standalone sender's no-network test doubles."""

import sys
from types import SimpleNamespace


class TelegramError(Exception):
    """SDK-compatible base class without requiring the optional Telegram package."""


def install_error_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "telegram.error", SimpleNamespace(TelegramError=TelegramError))
