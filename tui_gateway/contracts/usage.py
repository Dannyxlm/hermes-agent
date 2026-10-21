"""Native usage/API-value and new-chat defaults contracts."""
from typing import Literal

from pydantic import Field, RootModel

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
    window: Literal["today", "7d", "30d", "all"] = "7d"
    timezone: str = "UTC"  # IANA zone; calendar windows and chart use the same zone
    session_id: str | None = None  # stored ID, never a runtime ID
    limit: int = Field(default=20, ge=1, le=100, strict=True)


class UsageTokenMetrics(Result):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    api_call_count: int
    total_tokens: int
    cache_hit_percent: float | None
    estimated_cost_usd: float | None
    known_estimated_cost_usd: float
    unknown_price_events: int
    cost_reason: str | None
    pricing_dates: list[str]
    price_sources: list[str]


class UsageMetrics(UsageTokenMetrics):
    cost_status: Literal["est", "unknown"]
    turn_count: int
    event_count: int
    unattributed_turn_events: int


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


class AllTimeMetrics(UsageTokenMetrics):
    minimum_estimated_cost_usd: float
    cost_status: Literal["est", "minimum", "unknown"]
    turn_count: None
    event_count: None
    unattributed_turn_events: None


class AllTimeAgentUsage(AllTimeMetrics):
    profile: str


class AllTimeChatUsage(AllTimeMetrics):
    session_id: str
    title: str | None


class AllTimeModelUsage(AllTimeMetrics):
    model: str
    provider: str


class AllTimeProviderUsage(AllTimeMetrics):
    provider: str


class ProviderCoverage(Result):
    provider: str
    since: float
    chat_count: int
    session_count: int


class AllTimeUsageRollupResult(Result):
    profile: str
    window: Literal["all"]
    source: Literal["session_totals"]
    start: float | None
    end: float
    timezone: str
    totals: AllTimeMetrics
    agents: list[AllTimeAgentUsage]
    models: list[AllTimeModelUsage]
    providers: list[AllTimeProviderUsage]
    days: list[DailyUsage] = Field(max_length=0)
    top_chats: list[AllTimeChatUsage]
    chat_count: int
    coverage: list[ProviderCoverage]
    coverage_since: float | None
    partial: Literal[True]
    coverage_reason: str
    basis: Literal["standard_api_list_price"]
    currency: Literal["USD"]


class UsageRollupWireResult(RootModel[UsageRollupResult | AllTimeUsageRollupResult]):
    pass


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
method("usage.rollup", params=UsageRollupParams, result=UsageRollupWireResult,
       doc="Read one profile's timestamped windows or retained all-time session totals; no transcript scan.")
method("config.defaults.get", params=DefaultsGetParams, result=DefaultsResult,
       doc="Read profile-wide new-chat reasoning/delegation defaults, separate from session controls.")
method("config.defaults.set", params=DefaultsSetParams, result=DefaultsSetResult,
       doc="Confirmed authenticated new-chat default write, audited and state-idempotent, with disk readback.")
