"""Scoped native Bot Chat and notification RPCs (methods_mobile*.py)."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import Params, Result
from .common import PendingApproval, TranscriptMessage
from .prompt_voice import PromptSubmitResult
from .registry import method
from .server_requests import ClarifyQuestion
from .sessions import InflightTurn, OpenRequestEntry, QueuedPrompt, SessionInterruptResult, TodoState


class MobileEmptyParams(Params):
    pass


class MobileCapabilitiesResult(Result):
    protocol_version: int
    methods: list[str]
    features: list[str]
    max_history_limit: int
    max_roster_limit: int


class MobileBotsParams(Params):
    limit: int = 50
    offset: int = 0


class MobileBotMeta(Result):
    title: str | None = None
    description: str | None = None
    color: str | None = None
    shape: str | None = None
    emoji: str | None = None


class MobileBotUIMeta(Result):
    hermes_bots: MobileBotMeta = Field(alias="hermes-bots")


class MobileCanonicalSession(Result):
    id: str
    resolved_id: str
    preview: str | None = None
    last_active: float
    message_count: int


class MobileBot(Result):
    profile: str
    display_name: str
    description: str
    title: str
    ui_meta: MobileBotUIMeta
    canonical_session: MobileCanonicalSession | None
    has_avatar: bool
    working: bool | None
    unavailable_reason: Literal["canonical_unavailable"] | None = None


class MobileBotsResult(Result):
    bots: list[MobileBot]
    next_offset: int | None


class MobileOpenParams(Params):
    profile: str
    canonical_root_id: str
    limit: int = 50
    before_row_id: int | None = None


class MobileScopeParams(Params):
    profile: str
    canonical_root_id: str
    session_id: str


class MobileSnapshotParams(MobileScopeParams):
    limit: int = 50
    before_row_id: int | None = None


class MobileHistory(Result):
    messages: list[TranscriptMessage]
    has_more: bool
    before_row_id: int | None


class MobilePendingClarify(Result):
    request_id: str
    session_id: str | None = None
    question: str | None = None
    choices: list[str] | None = None
    multi_select: bool | None = None
    questions: list[ClarifyQuestion] | None = None
    answers: dict[str, str] | None = None
    mobile_supported: bool | None = None
    owning_client: str | None = None


class MobileNotificationRun(Result):
    run_id: str
    status: str
    started_at: float
    updated_at: float


class MobileSnapshotResult(Result):
    profile: str
    canonical_root_id: str
    session_id: str
    stored_session_id: str
    history: MobileHistory
    running: bool
    status: str
    inflight: InflightTurn | None
    queued: QueuedPrompt | None
    pending_approvals: list[PendingApproval]
    pending_clarify: MobilePendingClarify | None
    notification_run: MobileNotificationRun | None
    open_requests: list[OpenRequestEntry] | None = None
    todo_state: TodoState | None = None


class MobileSubmitParams(MobileScopeParams):
    text: str


class MobileSubmitResult(PromptSubmitResult):
    accepted: bool


class MobileApprovalParams(MobileScopeParams):
    request_id: str
    choice: str  # handler rejects choices outside the exact pending request's offered set


class MobileApprovalResult(Result):
    resolved: int


class MobileClarifyParams(MobileScopeParams):
    request_id: str
    answer: str
    question_id: str = ""


class MobileClarifyResult(Result):
    status: Literal["ok", "expired"]
    remaining: list[str] | None = None


class MobilePushStatusResult(Result):
    available: bool
    protocol_version: int


class MobileRegistrationParams(Params):
    installation_id: str
    connection_id: str
    environment: Literal["production", "sandbox"]


class MobilePushRefreshParams(MobileRegistrationParams):
    device_token: str
    categories: list[Literal["attention", "completion"]] = Field(default_factory=lambda: ["attention", "completion"])
    preview_enabled: bool = False


class MobilePushRegisterParams(MobilePushRefreshParams, MobileScopeParams):
    pass


class MobileActivityRefreshParams(MobileRegistrationParams):
    activity_token: str
    activity_id: str
    run_id: str


class MobileActivityRegisterParams(MobileActivityRefreshParams, MobileScopeParams):
    pass


class MobileRegistrationResult(Result):
    subscription_id: str
    expires_at: float


class MobileRefreshResult(Result):
    updated: int
    subscriptions: list[MobileRegistrationResult]


class MobileUnregisterParams(Params):
    installation_id: str
    connection_id: str
    subscription_id: str | None = None


class MobileActivityUnregisterParams(MobileUnregisterParams):
    subscription_id: str


class MobileUnregisterResult(Result):
    removed: int


class MobileStoredSessionParams(Params):
    """Separate authority from canonical Bot Chat; exact stored ID, not title or runtime ID."""
    profile: str
    stored_session_id: str


class MobileSessionPushRegisterParams(MobilePushRefreshParams, MobileStoredSessionParams):
    pass


class MobileSessionActivityRegisterParams(MobileActivityRefreshParams, MobileStoredSessionParams):
    pass


class MobileWidgetRegisterParams(MobileRegistrationParams, MobileStoredSessionParams):
    read_token: str  # client-generated 32 random bytes as lowercase hex; never a general bearer
    widget_token: str = ""


class MobileSessionDestination(Result):
    surface: Literal["native_session"]
    profile: str
    session_id: str  # compression lineage root


class MobileSessionRegistrationResult(MobileRegistrationResult):
    destination: MobileSessionDestination
    resolved_session_id: str
    notification_run: MobileNotificationRun | None
    snapshot_path: str | None = None


class MobileSessionPresenceParams(MobileStoredSessionParams):
    installation_id: str
    connection_id: str
    foreground: bool


class MobileSessionPresenceResult(Result):
    foreground: bool
    expires_at: float


method("mobile.session_push.register", params=MobileSessionPushRegisterParams, result=MobileSessionRegistrationResult)
method("mobile.session_push.refresh", params=MobilePushRefreshParams, result=MobileRefreshResult)
method("mobile.session_push.unregister", params=MobileUnregisterParams, result=MobileUnregisterResult)
method("mobile.session_push.presence", params=MobileSessionPresenceParams, result=MobileSessionPresenceResult,
       doc="Renew a 60-second foreground lease for a registered ordinary destination; affects alerts only.")
method("mobile.session_activity.register", params=MobileSessionActivityRegisterParams, result=MobileSessionRegistrationResult)
method("mobile.session_activity.refresh", params=MobileActivityRefreshParams, result=MobileRefreshResult)
method("mobile.session_activity.unregister", params=MobileActivityUnregisterParams, result=MobileUnregisterResult)
method("mobile.widget.register", params=MobileWidgetRegisterParams, result=MobileSessionRegistrationResult,
       doc="Register or rotate an ordinary-session widget lease and exact GET snapshot capability.")
method("mobile.widget.unregister", params=MobileUnregisterParams, result=MobileUnregisterResult)


method("mobile.capabilities", params=MobileEmptyParams, result=MobileCapabilitiesResult)
method("mobile.bots", params=MobileBotsParams, result=MobileBotsResult)
method("mobile.open", params=MobileOpenParams, result=MobileSnapshotResult)
method("mobile.snapshot", params=MobileSnapshotParams, result=MobileSnapshotResult)
method("mobile.submit", params=MobileSubmitParams, result=MobileSubmitResult)
method("mobile.stop", params=MobileScopeParams, result=SessionInterruptResult)
method("mobile.approval.respond", params=MobileApprovalParams, result=MobileApprovalResult)
method("mobile.clarify.respond", params=MobileClarifyParams, result=MobileClarifyResult)
method("mobile.push.status", params=MobileEmptyParams, result=MobilePushStatusResult)
method("mobile.push.register", params=MobilePushRegisterParams, result=MobileRegistrationResult)
method("mobile.push.refresh", params=MobilePushRefreshParams, result=MobileRefreshResult)
method("mobile.push.unregister", params=MobileUnregisterParams, result=MobileUnregisterResult)
method("mobile.activity.register", params=MobileActivityRegisterParams, result=MobileRegistrationResult)
method("mobile.activity.refresh", params=MobileActivityRefreshParams, result=MobileRefreshResult)
method("mobile.activity.unregister", params=MobileActivityUnregisterParams, result=MobileUnregisterResult)
