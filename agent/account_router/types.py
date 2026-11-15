# agent/account_router/types.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

@dataclass(frozen=True)
class MeterWindow:
    kind: str                       # "session" | "weekly" | "period"
    used_pct: Optional[float]       # 0..100, None = unknown
    resets_at: Optional[float]      # epoch seconds, None = unknown
    model_scope: Optional[str] = None  # None = whole account; else a family key such as "fable", "opus", "sonnet"

@dataclass(frozen=True)
class AccountMeter:
    provider: str                   # "anthropic" | "openai-codex" | "xai-oauth"
    entry_id: str                   # pool row id
    plan: Optional[str]             # provider plan label, e.g. "default_claude_max_20x", "pro", "prolite", "plus", "GrokPro"
    windows: tuple[MeterWindow, ...]
    fetched_at: float               # epoch seconds
    source: str                     # "usage_api" | "headers"
    extras: dict = field(default_factory=dict)  # e.g. {"banked_resets": 3, "credits": 57155.5}
