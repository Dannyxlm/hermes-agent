"""Additive Hermex live discovery, learned changes and exact history jumps."""
from typing import Literal

from pydantic import Field

from .base import JsonValue, Params, Result
from .registry import method


class LiveProfileParams(Params):
    profile: str = Field(min_length=1, max_length=64, strict=True)


class SkillChangesListParams(LiveProfileParams):
    session_id: str = Field(min_length=1, max_length=256, strict=True)
    cursor: str | None = Field(default=None, max_length=32768)
    limit: int = Field(default=20, ge=1, le=100, strict=True)


class SkillChangeParams(LiveProfileParams):
    change_id: str = Field(min_length=1, max_length=64, strict=True)


class SkillChangeDecideParams(SkillChangeParams):
    decision: Literal["keep", "undo"]
    expected_revision: str = Field(min_length=1, max_length=256, strict=True)
    client_action_id: str = Field(min_length=1, max_length=128, strict=True)


class SkillChangeLine(Result):
    kind: Literal["add", "remove"]
    text: str


class SkillChangeItem(Result):
    change_id: str
    skill: str
    session_id: str
    anchor_message_id: str | None
    created_at: str
    revision: str
    title: str | None
    reason: str | None
    lines: list[SkillChangeLine]
    decision: Literal["unreviewed", "kept", "undone"]
    can_undo: bool


class SkillChangesListResult(Result):
    version: Literal[1]
    profile: str
    items: list[SkillChangeItem]
    next_cursor: str | None


class SkillChangeResult(Result):
    version: Literal[1]
    change_id: str
    revision: str
    diff: str | None
    can_undo: bool
    decision: Literal["unreviewed", "kept", "undone"]
    receipt_id: str | None


class SkillChangeDecisionResult(Result):
    change_id: str
    decision: Literal["kept", "undone"]
    revision: str
    receipt_id: str


class LiveSearchParams(LiveProfileParams):
    query: str = Field(min_length=1, max_length=4096, strict=True)
    session_id: str | None = Field(default=None, min_length=1, max_length=256)
    cursor: str | None = Field(default=None, max_length=32768)
    limit: int = Field(default=20, ge=1, le=100, strict=True)


class LiveSearchRange(Result):
    start: int
    length: int


class LiveSearchMatch(Result):
    message_id: str
    logical_id: str
    source_session_id: str
    role: Literal["user", "assistant"]
    snippet: str
    ranges: list[LiveSearchRange]


class LiveSearchGroup(Result):
    session_id: str
    lineage_root: str
    matches: list[LiveSearchMatch]
    match_count: int | None


class LiveSearchResult(Result):
    version: Literal[1]
    profile: str
    query: str
    revision: str
    results: list[LiveSearchGroup]
    next_cursor: str | None


class LiveWindowParams(LiveProfileParams):
    session_id: str = Field(min_length=1, max_length=256, strict=True)
    message_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$", strict=True)
    expected_revision: str = Field(min_length=1, max_length=256, strict=True)
    limit: int = Field(default=100, ge=1, le=120, strict=True)
    cursor: str | None = Field(default=None, max_length=32768)


class LiveWindowResult(Result):
    version: Literal[1]
    profile: str
    session_id: str
    revision: str
    anchor_message_id: str
    messages: list[dict[str, JsonValue]]
    older_cursor: str | None
    newer_cursor: str | None
    offset: int | None


method("skills.changes.list", params=SkillChangesListParams, result=SkillChangesListResult,
       doc="Committed skill changes in an exact owned conversation lineage.")
method("skills.changes.get", params=SkillChangeParams, result=SkillChangeResult,
       doc="Recorded skill diff and durable decision readback.")
method("skills.changes.decide", params=SkillChangeDecideParams, result=SkillChangeDecisionResult,
       doc="Acknowledge or conflict-safely undo a recorded skill edit.")
method("session.search", params=LiveSearchParams, result=LiveSearchResult,
       doc="Paged message IDs and UTF-16 snippet ranges in owned history.")
method("session.messages.window", params=LiveWindowParams, result=LiveWindowResult,
       doc="Bounded display window at an exact message, including compression ancestors.")
