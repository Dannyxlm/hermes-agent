"""Read-only owner access to the team-room JSON archive."""
from typing import Annotated, Literal

from pydantic import Field

from .base import Params, Result
from .registry import method

MeetingId = Annotated[str, Field(strict=True, max_length=32, pattern=r'^\d{4}-\d{2}-\d{2}-(standup|campfire)$')]


class TeamRoomListParams(Params):
    limit: int = Field(default=30, ge=1, le=60, strict=True)
    before: MeetingId | None = None


class TeamRoomGetParams(Params):
    id: MeetingId


class TeamRoomMetadata(Result):
    id: str
    kind: Literal['standup', 'campfire']
    date: str
    started: str
    host: str
    topic: str | None
    participants: list[str]


class TeamRoomSummary(TeamRoomMetadata):
    message_count: int
    preview: str


class TeamRoomMessage(Result):
    name: str
    text: str
    at: str


class TeamRoomKept(Result):
    name: str
    text: str


class TeamRoomGetResult(TeamRoomMetadata):
    messages: list[TeamRoomMessage]
    kept: list[TeamRoomKept]


class TeamRoomListResult(Result):
    meetings: list[TeamRoomSummary]
    next_before: str | None


method('team_room.list', params=TeamRoomListParams, result=TeamRoomListResult,
       params_error=(4400, 'Invalid team room request'))
method('team_room.get', params=TeamRoomGetParams, result=TeamRoomGetResult,
       params_error=(4400, 'Invalid team room request'))
