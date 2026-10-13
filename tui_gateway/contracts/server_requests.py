"""Server→client requests: the backend asks the renderer a question (``server_requests.send``).

Every entry is one request method: the ``params`` the frame carries (``session_id`` is added by
the transport and declared on the shared base) and the ``result`` the client answers with. The
``request.cancel`` event that withdraws an open request lives here too.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from tools.tour_presets import TourPreset

from .base import JsonValue, Params, Payload, Result, WireEnum
from .registry import event, server_request


class ServerRequestParams(Params):
    """Every request frame's params start with the session the question belongs to."""

    session_id: str
    created_at: float | None = None
    expires_at: float | None = None
    timeout_seconds: float | None = None
    server_time: float | None = None
    request_epoch: str | None = None
    request_revision: int | None = None


class ValueResult(Result):
    """The answer to any one-string prompt (sudo, secret, vault prompts, desktop bridges):
    ``''`` means skipped / declined."""

    value: str


# ── clarify ───────────────────────────────────────────────────────────────────────────────────


class ClarifyQuestion(Params):
    qid: str
    question: str
    choices: list[str] | None = None
    multi_select: bool = False


class ClarifyRequestParams(ServerRequestParams):
    """``answers`` rides only on a reconnect replay (locks the server already accepted; null = skipped)."""

    questions: list[ClarifyQuestion]
    answers: dict[str, str | None] | None = None


class ClarifyResult(Result):
    """``{answers}`` for the whole set (early locks go through the ``clarify.lock`` RPC); a response
    without ``answers`` is cancel-all."""

    answers: dict[str, str | None] | None = None


server_request("clarify", params=ClarifyRequestParams, result=ClarifyResult,
               doc="The clarify tool: ask the user 1-5 questions.")


class SetupChooseKind(WireEnum):
    question = "question"
    accent = "accent"
    theme = "theme"
    layout = "layout"
    connectors = "connectors"
    plugins = "plugins"
    tour = "tour"
    fork = "fork"
    machine_use = "machine_use"


class SetupChooseOption(Params):
    id: str
    label: str
    detail: str | None = None


class SetupChooseRequestParams(ServerRequestParams):
    kind: SetupChooseKind
    question: str
    options: list[SetupChooseOption] | None = None
    multi_select: bool = False
    # Row ids the card starts with picked: the plugins the setup facts found (Blender).
    preselected: list[str] | None = None


class SetupChooseResult(Result):
    picked: str | list[str] | None = None
    # The name the card showed for each picked row (one per id in ``picked``); the model says it, not the id.
    label: str | list[str] | None = None
    # Composer text that named no row of the card: the user's words, never a pick.
    said: str | None = None


server_request("setup_choose", params=SetupChooseRequestParams, result=SetupChooseResult)


# ── approval ──────────────────────────────────────────────────────────────────────────────────


class ApprovalChoice(WireEnum):
    once = "once"
    session = "session"
    always = "always"
    deny = "deny"


class ApprovalKind(WireEnum):
    terminal = "terminal"
    tool = "tool"
    payment = "payment"
    mcp_trust = "mcp_trust"
    mcp_elicitation = "mcp_elicitation"

    @classmethod
    def for_context(cls, surface: str | None, tool_name: str | None = None, *, payment: bool = False) -> ApprovalKind:
        if surface == "vault-payment" or payment:
            return cls.payment
        family = (surface or "").split("/", 1)[0]
        if family == "mcp-trust":
            return cls.mcp_trust
        if family == "mcp-elicitation":
            return cls.mcp_elicitation
        return cls.tool if tool_name and tool_name != "terminal" else cls.terminal


class PaymentMerchant(Params):
    name: str
    source: Literal["page"]


class PaymentTotal(Params):
    amount_minor: int
    currency: str | None = None
    display: str
    source: Literal["page"]
    label: str


class PaymentLine(Params):
    name: str
    qty: int
    amount_minor: int


class PaymentAdjustment(Params):
    kind: Literal["discount", "tax", "shipping", "fee"]
    amount_minor: int


class PaymentLimit(Params):
    amount_minor: int
    currency: str | None = None
    period: Literal["per_purchase", "monthly", "unknown"]
    source: Literal["config", "vault"]


class PaymentCard(Params):
    """Presentation metadata only; never PAN, expiry or CVC."""

    label: str
    last4: str | None = Field(default=None, pattern=r"^[0-9]{4}$")
    backend: str | None = None
    limit: PaymentLimit | None = None


class PaymentChecks(Params):
    total_found: bool
    over_limit: bool | None = None


class PaymentApprovalContext(Params):
    """Frozen Pay v1 metadata; page facts are not issuer or origin authority."""

    v: Literal[1]
    origin: str
    origin_bound: bool
    merchant: PaymentMerchant | None = None
    total: PaymentTotal | None = None
    lines: list[PaymentLine] = Field(default_factory=list)
    lines_truncated: bool = False
    adjustments: list[PaymentAdjustment] = Field(default_factory=list)
    card: PaymentCard
    checks: PaymentChecks


class ApprovalRequestParams(ServerRequestParams):
    """``tui_gateway/server.py::_approval_request_payload`` — the command is redacted server-side."""

    request_id: str
    kind: ApprovalKind | None = None
    # Explicit producer risk classification. Missing/old context is not safe quick consent.
    risk: Literal["read_only", "destructive", "sensitive", "unknown"] = "unknown"
    destructive: bool = False
    sensitive: bool = False
    # Output projection derives this; a producer-supplied True grants nothing.
    quick_once: bool = False
    payment: PaymentApprovalContext | None = None
    command: str = ""
    description: str = ""
    choices: list[ApprovalChoice] = Field(default_factory=list)
    allow_permanent: bool | None = None
    allow_session: bool | None = None
    smart_denied: bool | None = None
    tool_name: str | None = None
    surface: str | None = None
    workspace: str | None = None
    origin: str | None = None
    mcp_server: str | None = None
    session_id_hint: str | None = Field(default=None, alias="gateway_session_id")
    # The approval queue entry carries tool-specific context the card may render; the closed set
    # of keys is owned by tools/approval.py, so it stays open here.
    model_config = Params.model_config | {"extra": "allow"}

    @model_validator(mode="after")
    def consent_scopes(self) -> ApprovalRequestParams:
        inferred = ApprovalKind.for_context(self.surface, self.tool_name, payment=self.payment is not None)
        consent = {ApprovalKind.payment, ApprovalKind.mcp_trust, ApprovalKind.mcp_elicitation}
        if inferred in consent or self.kind in consent:
            if (any(choice not in (ApprovalChoice.once, ApprovalChoice.deny) for choice in self.choices)
                    or self.allow_session is True or self.allow_permanent is True):
                raise ValueError("Per-call consent is once-only")
        if self.payment is not None and self.origin is not None and self.origin != self.payment.origin:
            raise ValueError("Payment origin mismatch")
        return self


class ApprovalResult(Result):
    choice: ApprovalChoice
    all: bool | None = None
    reason: str | None = Field(default=None, max_length=2000)
    # Client-reported authentication is receipt metadata, never answer authority.
    auth: str | None = None

    @field_validator("auth")
    @classmethod
    def known_auth(cls, value: str | None) -> str | None:
        return value if value in ("face_id", "touch_id", "optic_id", "passcode") else None


server_request("approval", params=ApprovalRequestParams, result=ApprovalResult,
               doc="An action awaits the user's exact offered consent.")


# ── one-string prompts ────────────────────────────────────────────────────────────────────────


class EmptyRequestParams(ServerRequestParams):
    pass


class SudoRequestParams(ServerRequestParams):
    """Original command, redacted server-side before any password-injection rewrite."""

    command: str = ""


server_request("sudo", params=SudoRequestParams, result=ValueResult,
               doc="Masked sudo password for the terminal tool.")


class SecretRequestParams(ServerRequestParams):
    env_var: str
    prompt: str
    metadata: dict[str, JsonValue] | None = None


server_request("secret", params=SecretRequestParams, result=ValueResult,
               doc="Masked value for a named env var (skills / setup flows).")


class VaultUnlockRequestParams(ServerRequestParams):
    backend: str
    display_name: str


server_request("vault.unlock_prompt", params=VaultUnlockRequestParams, result=ValueResult,
               doc="Master password to unlock an external password manager for this session.")


class VaultSaveLoginRequestParams(ServerRequestParams):
    origin: str
    site: str


server_request("vault.save_login", params=VaultSaveLoginRequestParams, result=ValueResult,
               doc="Save a login for a site the agent is about to fill; the answer is JSON {identifier, password}.")


class VaultCodeRequestParams(ServerRequestParams):
    site: str | None = None
    hint: str | None = None


server_request("vault.code", params=VaultCodeRequestParams, result=ValueResult,
               doc="A one-time / 2FA code the user reads from their device.")


# ── desktop GUI bridges ───────────────────────────────────────────────────────────────────────


class ReadRangeRequestParams(ServerRequestParams):
    start: int | None = None
    count: int | None = None


server_request("terminal.read", params=ReadRangeRequestParams, result=ValueResult,
               doc="Read the visible in-app terminal buffer (JSON text answer).")
server_request("preview.read", params=ReadRangeRequestParams, result=ValueResult,
               doc="Read the in-app browser preview's text (JSON text answer).")
server_request("window.read", params=EmptyRequestParams, result=ValueResult,
               doc="Enumerate the native window below the app (JSON text answer).")


class PreviewActRequestParams(ServerRequestParams):
    """``tools/drive_preview_tool.py`` and ``tools/annotate_preview_tool.py`` field sets."""

    action: str
    ref: str | None = None
    selector: str | None = None
    text: str | None = None
    key: str | None = None
    submit: bool | None = None
    full: bool | None = None
    to: str | None = None
    amount: int | None = None
    max: int | None = None
    allow_shortcut: bool | None = None


server_request("preview.act", params=PreviewActRequestParams, result=ValueResult,
               doc="Click / type / scroll / annotate inside the in-app browser preview.")


class TourStep(Params):
    selector: str | None = None
    title: str | None = None
    text: str | None = None
    side: str | None = None
    model_config = Params.model_config | {"extra": "allow"}


class TourRequestParams(ServerRequestParams):
    """``tools/tour_tool.py`` field set."""

    action: str
    surface: str | None = None
    selector: str | None = None
    title: str | None = None
    text: str | None = None
    side: str | None = None
    steps: list[TourStep] | None = None
    step_index: int | None = None
    preset: TourPreset | None = None


server_request("tour", params=TourRequestParams, result=ValueResult,
               doc="Drive a guided tour highlight in the desktop renderer.")


# ── withdrawal ────────────────────────────────────────────────────────────────────────────────


class RequestCancelReason(WireEnum):
    timeout = "timeout"
    interrupted = "interrupted"
    shutdown = "shutdown"
    resolved = "resolved"
    session_closed = "session_closed"


class RequestCancelPayload(Payload):
    id: str
    method: str
    reason: str  # a RequestCancelReason value; callers in tools/approval may pass their own wording


event("request.cancel", RequestCancelPayload,
      doc="The backend withdrew an open server→client request; clear the matching card only.")
