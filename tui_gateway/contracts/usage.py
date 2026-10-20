"""Native usage/API-value and new-chat defaults contracts."""
from typing import Literal

from pydantic import Field

from .base import Params, Result
from .common import ProfileParams
from .registry import method


class UsageEstimateParams(Params):
    provider: str
    model: str
    input_tokens: int = Field(default=0, ge=0, strict=True)
    output_tokens: int = Field(default=0, ge=0, strict=True)
    cache_read_tokens: int = Field(default=0, ge=0, strict=True)
    cache_write_tokens: int = Field(default=0, ge=0, strict=True)


class ApiValue(Result):
    estimated_cost_usd: float | None
    status: Literal["est", "unknown"]
    reason: str | None
    pricing_date: str
    source_url: str | None
    basis: Literal["standard_api_list_price"]
    currency: Literal["USD"]


class UsageRollupParams(ProfileParams):
    window: Literal["today", "7d", "30d"] = "7d"
    timezone: str = "UTC"  # IANA zone; calendar windows and chart use the same zone
    session_id: str | None = None  # stored ID, never a runtime ID
    limit: int = Field(default=20, ge=1, le=100, strict=True)


class UsageMetrics(Result):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    api_call_count: int
    turn_count: int
    event_count: int
    unattributed_turn_events: int
    total_tokens: int
    cache_hit_percent: float | None
    estimated_cost_usd: float | None
    known_estimated_cost_usd: float
    unknown_price_events: int
    cost_status: Literal["est", "unknown"]
    cost_reason: str | None
    pricing_dates: list[str]
    price_sources: list[str]


class AgentUsage(UsageMetrics):
    profile: str


class ChatUsage(UsageMetrics):
    session_id: str
    title: str | None


class ModelUsage(UsageMetrics):
    model: str
    provider: str


class ProviderUsageTotals(UsageMetrics):
    provider: str


class DailyUsage(ProviderUsageTotals):
    day: str


class UsageRollupResult(Result):
    profile: str
    start: float
    end: float
    timezone: str
    totals: UsageMetrics
    agents: list[AgentUsage]
    models: list[ModelUsage]
    providers: list[ProviderUsageTotals]
    days: list[DailyUsage]
    top_chats: list[ChatUsage]
    chat_count: int
    coverage_since: float | None
    partial: bool
    coverage_reason: str
    basis: Literal["standard_api_list_price"]
    currency: Literal["USD"]


class DefaultsGetParams(Params):
    profile: str = Field(min_length=1)


class DefaultsSetParams(DefaultsGetParams):
    key: Literal["reasoning", "delegation"]
    value: str
    provider: str = ""
    confirm: bool = False


class DefaultsResult(Result):
    reasoning: str
    reasoning_levels: list[str]
    delegation_model: str
    delegation_provider: str
    main_model: str
    applies_to: Literal["new_chats_and_delegations"]
    reasoning_precedence: str


class DefaultsSetResult(DefaultsResult):
    changed: bool
    operation_id: str | None


method("usage.estimate", params=UsageEstimateParams, result=ApiValue,
       doc="Offline, sourced standard API value (est), independent of subscription billing.")
method("usage.rollup", params=UsageRollupParams, result=UsageRollupResult,
       doc="Read indexed timestamped usage for one profile; no transcript scan or historical inference.")
method("config.defaults.get", params=DefaultsGetParams, result=DefaultsResult,
       doc="Read profile-wide new-chat reasoning/delegation defaults, separate from session controls.")
method("config.defaults.set", params=DefaultsSetParams, result=DefaultsSetResult,
       doc="Confirmed authenticated new-chat default write, audited and state-idempotent, with disk readback.")
