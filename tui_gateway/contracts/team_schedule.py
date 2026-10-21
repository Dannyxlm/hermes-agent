"""Owner-only Team and schedule wire contracts. Unknown coverage stays explicit."""
from typing import Literal

from pydantic import Field

from .base import Params, Result
from .registry import method


class TeamActivityParams(Params):
    profiles: list[str] = Field(min_length=1, max_length=100)


class TeamChat(Result):
    profile: str
    stored_session_id: str


class TeamCurrent(Result):
    kind: str
    title: str | None
    work_label: str | None
    started_at: float | None
    open_chat: TeamChat | None


class TeamAgentActivity(Result):
    profile: str
    working: bool | None
    last_human_chat_at: float | None
    current: TeamCurrent | None


class TeamActivityResult(Result):
    version: Literal[1]
    now: float
    complete: bool
    working_count: int | None
    agents: list[TeamAgentActivity]


class CronSummaryParams(TeamActivityParams):
    timezone: str = Field(min_length=1, max_length=128)
    start: float = Field(strict=True, allow_inf_nan=False)
    end: float = Field(strict=True, allow_inf_nan=False)
    limit: int = Field(default=50, ge=1, le=100, strict=True)
    cursor: str | None = Field(default=None, max_length=2048)


class CronSummaryCounts(Result):
    expected: int | None
    succeeded: int | None
    retried: int | None
    needs_attention: int | None


class CronSummaryRun(Result):
    profile: str
    job_id: str
    occurrence_id: str
    started_at: float | None
    finished_at: float | None
    outcome: str
    retry_of: str | None
    delivery_summary: str | None


class CronSummaryResult(Result):
    version: Literal[1]
    timezone: str
    start: float
    end: float
    complete: bool
    counts: CronSummaryCounts
    runs: list[CronSummaryRun]
    next_cursor: str | None


method("team.activity", params=TeamActivityParams, result=TeamActivityResult,
       doc="Owner-scoped verified positive runtime activity; unknown coverage never means free.")
method("cron.summary", params=CronSummaryParams, result=CronSummaryResult,
       doc="Read-only bounded execution receipts with nullable aggregate coverage and scope-bound paging.")
