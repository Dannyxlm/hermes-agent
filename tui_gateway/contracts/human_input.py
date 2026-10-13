"""Version 1 owner-scoped human-input queries/actions (REST and native RPC)."""
from pydantic import Field
from .base import Params, Result, JsonValue, WireEnum
from .registry import method


class HumanInputDestination(WireEnum):
    chat = 'chat'
    bot = 'bot'
    connection = 'connection'


class HumanInputScope(Params):
    profile: str = Field(min_length=1, max_length=64)
    connection_id: str = Field(min_length=1, max_length=256)
    installation_id: str = Field(min_length=1, max_length=256)
    destination_kind: HumanInputDestination
    session_id: str = Field(max_length=256)
    stored_session_id: str = Field(default='', max_length=256)
    canonical_root_id: str = Field(default='', max_length=256)


class HumanInputActionStatusParams(HumanInputScope):
    request_id: str = Field(min_length=1, max_length=256)
    method: str = Field(min_length=1, max_length=64)
    request_epoch: str = Field(min_length=1, max_length=128)
    request_revision: int = Field(ge=1)
    action_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    question_id: str | None = Field(default=None, max_length=256)


class HumanInputRespondParams(HumanInputActionStatusParams):
    # Validated against the exact server request's method-specific contract before consumption.
    result: dict[str, JsonValue]


class HumanInputStatus(WireEnum):
    accepted = 'accepted'
    already_resolved = 'already_resolved'
    expired = 'expired'
    cancelled = 'cancelled'
    conflict = 'conflict'
    unknown = 'unknown'
    outcome_unknown = 'outcome_unknown'
    unavailable = 'unavailable'


class HumanInputReceipt(Result):
    protocol_version: int = 1
    status: HumanInputStatus
    action_id: str
    request_id: str
    method: str
    request_epoch: str
    request_revision: int
    server_time: float


class HumanInputPendingRequest(Result):
    id: str
    method: str
    params: dict[str, JsonValue]


class HumanInputPendingResult(Result):
    protocol_version: int = 1
    request_epoch: str
    server_time: float
    complete: bool
    unavailable: list[str]
    requests: list[HumanInputPendingRequest]


method('human_input.pending', params=HumanInputScope, result=HumanInputPendingResult)
method('human_input.respond', params=HumanInputRespondParams, result=HumanInputReceipt)
method('human_input.action_status', params=HumanInputActionStatusParams, result=HumanInputReceipt)
