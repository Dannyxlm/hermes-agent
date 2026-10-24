"""Family v1 metadata projections shared by legacy and canonical native transports."""
from typing import Literal

from pydantic import Field

from .base import Params, Result
from .registry import canonical_method, method


class FamilyCapability(Result):
    protocol_version: Literal[1] = 1


class FamilySessionParams(Params):
    session_id: str
    profile: str
    since: float | None = Field(default=None, allow_inf_nan=False)
    limit: int | None = Field(default=None, ge=1, le=500)


class FamilyOverviewParams(Params):
    since: float | None = Field(default=None, allow_inf_nan=False)
    coming_up_within: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class FamilyNeedsYou(Result):
    kind: Literal["approval", "clarify"]
    session_id: str
    profile: str


class FamilyNode(Result):
    node_id: str
    parent_node_id: str | None
    kind: str
    profile: str
    session_id: str | None
    title: str | None
    full_title: str | None
    batch_id: str | None
    status: Literal["working", "needs_you", "failed", "done", "paused_by_restart", "stopped", "quiet", "unknown"]
    step_kind: str | None
    step_tool: str | None
    activity_text: str | None
    started_at: float | None
    ended_at: float | None
    last_activity_at: float | None
    model: str | None
    terminal_cause: str | None
    needs_you: FamilyNeedsYou | None
    next_run_at: float | None
    exit_code: int | None
    command: str | None
    report_hint: str | None
    child_count: int
    backend_ref: str | None
    root_session_id: str | None
    root_profile: str | None
    actions: list[Literal["open", "answer", "resume", "stop"]]


class FamilyBatch(Result):
    batch_id: str
    batch_name: str | None
    parent_node_id: str
    created_at: float
    size: int


class FamilySessionResult(Result):
    protocol_version: Literal[1]
    root: FamilyNode
    nodes: list[FamilyNode]
    batches: list[FamilyBatch]
    complete: bool
    unavailable_profiles: list[str]
    truncated: bool
    generated_at: float


class FamilyTeamMember(Result):
    profile: str
    state: Literal["working", "needs_you", "resting"]
    live_count: int


class FamilyRoot(Result):
    root: FamilyNode
    live_leaves: list[FamilyNode]
    live_count: int
    total_count: int
    needs_you_count: int
    failed_count: int


class FamilyEarlier(Result):
    root: FamilyNode
    total_count: int
    failed_count: int
    ended_at: float


class FamilyOverviewResult(Result):
    protocol_version: Literal[1]
    team: list[FamilyTeamMember]
    needs_you: list[FamilyNode]
    failed: list[FamilyNode]
    roots: list[FamilyRoot]
    coming_up: list[FamilyNode]
    earlier: list[FamilyEarlier]
    complete: bool
    unavailable_profiles: list[str]
    generated_at: float


for register in (method, canonical_method):
    register("session.family", params=FamilySessionParams, result=FamilySessionResult)
    register("family.overview", params=FamilyOverviewParams, result=FamilyOverviewResult)
