"""Owner-scoped shared Desktop resources. Control is unavailable without a fenced backend."""
from typing import Literal

from pydantic import Field, model_validator

from .base import Params, Result
from .registry import method


class DesktopScope(Params):
    profile: str = Field(min_length=1, max_length=64)
    space: Literal['desktop', 'studio']
    connection_id: str = Field(min_length=1, max_length=256)
    installation_id: str = Field(min_length=1, max_length=256)


class DesktopLease(Result):
    holder: Literal['agent', 'human', 'none']
    epoch: int = Field(ge=0)
    mine: bool


class DesktopActive(Result):
    id: str
    agent: str | None
    title: str | None
    stored_session_id: str | None
    started_at: float | None


class DesktopSignIn(Result):
    id: str
    revision: int = Field(ge=1)
    expires_at: float
    origin: str
    reason: str
    allowed_actions: list[str]
    login_handle: str | None
    masked_identifier: str | None


class DesktopActivity(Result):
    id: str
    label: str
    at: float


class DesktopRecording(Result):
    id: str
    title: str
    agent: str | None
    chat_title: str | None
    started_at: float
    duration_seconds: float | None
    thumbnail_path: str | None
    replay_available: bool


class DesktopSnapshotParams(DesktopScope):
    cursor: None = None
    limit: int = Field(default=20, ge=1, le=100, strict=True)


class DesktopSnapshot(Result):
    protocol_version: Literal[1]
    resource_id: str
    revision: int = Field(ge=1)
    server_time: float
    state: Literal['watch', 'signin', 'takeover', 'idle']
    complete: bool
    unavailable: list[str]
    lease: DesktopLease
    active: DesktopActive | None
    signin: DesktopSignIn | None
    activity: list[DesktopActivity]
    history: list[DesktopRecording]
    next_cursor: str | None


class DesktopObserveParams(DesktopScope):
    mode: Literal['watch', 'control', 'replay']
    viewer_id: str | None = Field(default=None, min_length=1, max_length=128)
    recording_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_lease_epoch: int = Field(ge=0, strict=True)

    @model_validator(mode='after')
    def recording_matches_mode(self):
        if (self.mode == 'replay') != (self.recording_id is not None):
            raise ValueError('recording_id is required only for replay')
        return self


class DesktopObservation(Result):
    ticket: str
    viewer_id: str
    url: str
    expires_at: float
    mode: Literal['watch', 'control', 'replay']
    lease_epoch: int


class DesktopActionStatusParams(DesktopScope):
    action_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')


class DesktopActionParams(DesktopActionStatusParams):
    expected_revision: int = Field(ge=1, strict=True)
    expected_lease_epoch: int = Field(ge=0, strict=True)
    action: Literal['take_over', 'hand_back', 'saved_login', 'paste_login', 'navigate_back', 'not_now', 'open_page']
    viewer_id: str | None = Field(default=None, min_length=1, max_length=128)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    request_revision: int | None = Field(default=None, ge=1, strict=True)
    login_handle: str | None = Field(default=None, min_length=1, max_length=128)
    url: str | None = Field(default=None, min_length=1, max_length=2048)

    @model_validator(mode='after')
    def exact_action_fields(self):
        required = {
            'take_over': {'viewer_id'}, 'hand_back': {'viewer_id'},
            'saved_login': {'request_id', 'request_revision', 'login_handle'},
            'paste_login': {'viewer_id', 'request_id', 'request_revision', 'login_handle'},
            'navigate_back': {'viewer_id'}, 'not_now': {'request_id', 'request_revision'},
            'open_page': {'url'},
        }
        supplied = {key for key in ('viewer_id', 'request_id', 'request_revision', 'login_handle', 'url')
                    if getattr(self, key) is not None}
        if supplied != required[self.action]:
            raise ValueError('Fields do not match the desktop action')
        return self


class DesktopReceipt(Result):
    action_id: str
    status: Literal['accepted', 'conflict', 'expired', 'denied', 'outcome_unknown', 'unknown']
    revision: int
    lease_epoch: int
    server_time: float


method('desktop.snapshot', params=DesktopSnapshotParams, result=DesktopSnapshot)
method('desktop.observe', params=DesktopObserveParams, result=DesktopObservation)
method('desktop.action', params=DesktopActionParams, result=DesktopReceipt)
method('desktop.action_status', params=DesktopActionStatusParams, result=DesktopReceipt)
