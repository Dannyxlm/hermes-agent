"""Scoped alert and Live Activity projections; private text only for opted-in devices.

Copy for previews-off states lives in ``GENERIC_ALERT_BODIES`` and ``generic_activity_line`` so the
round-9 content contract (DECISIONS-round9 §6) can be applied in one place.
"""

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass

TOPIC = "co.cloudseed.hermex.ava"
SWIFT_EPOCH = 978307200
TERMINAL = frozenset({"complete", "failed", "cancelled"})
ATTENTION = frozenset({"waitingForApproval", "waitingForClarification"})
LABELS = {
    "starting": "Starting", "thinking": "Working", "usingTool": "Working with tools",
    "responding": "Writing a reply", "waitingForApproval": "Approval needed",
    "waitingForClarification": "Answer needed", "complete": "Complete",
    "failed": "Needs attention", "cancelled": "Stopped",
}
CONTENT_VERSION = 2
GENERIC_TITLE = "Hermex Ava"
INPUT_KINDS = {"waitingForApproval": "approval", "waitingForClarification": "clarification"}
# Allowlisted orb identities. Anything else carries no agent id and the app shows Ava's orb.
AGENT_IDS = {"default": "ava", "ava": "ava", "matisse": "matisse", "atlas": "atlas",
             "iris": "iris", "nuru": "nuru", "vox": "vox"}
GENERIC_ALERT_BODIES = {
    "complete": "Your reply is ready.",
    "failed": "Something went wrong with this run.",
    "cancelled": "The run has stopped.",
    "waitingForApproval": "Needs your approval.",
    "waitingForClarification": "Has a question for you.",
}
CLOSED_LINES = {"superseded": "Ended", "orphaned": "Interrupted"}


@dataclass(frozen=True)
class Scope:
    surface: str
    profile: str
    session_id: str

    def __post_init__(self):
        if self.surface not in {"native", "chats", "native_session"}:
            raise ValueError("invalid surface")
        for value in (self.profile, self.session_id):
            if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
                raise ValueError("invalid canonical scope")

    @property
    def key(self):
        return hashlib.sha256("\0".join((self.surface, self.profile, self.session_id)).encode()).hexdigest()


def agent_id(profile):
    return AGENT_IDS.get(profile.lower()) if isinstance(profile, str) else None


def agent_name(profile):
    """A display name for generic copy; never a chat title."""
    ident = agent_id(profile)
    return ident.capitalize() if ident else "Ava"


def preview_text(value, limit):
    """A bounded display excerpt, never serialized objects or hidden control text."""
    if not isinstance(value, str):
        return ""
    value = value[:8192]
    value = re.sub(r"```.*?(?:```|$)", " ", value, flags=re.S)
    value = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"(?m)^\s{0,3}(?:#{1,6}\s+|>\s*|[-*+]\s+)", "", value)
    value = re.sub(r"(\*\*|__|~~|`)(.+?)\1", r"\2", value)
    value = "".join(c for c in value if unicodedata.category(c) not in {"Cc", "Cf"} or c.isspace())
    value = " ".join(value.split())
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def first_line(value, limit):
    """The first non-empty display line of a reply (code fences never count)."""
    if not isinstance(value, str):
        return ""
    value = re.sub(r"```.*?(?:```|$)", "\n", value[:8192], flags=re.S)
    for line in value.splitlines():
        text = preview_text(line, limit)
        if text:
            return text
    return ""


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 999 else 0


@dataclass(frozen=True)
class AlertPreview:
    """Everything a device may show about a run. Text fields are private; counts are not."""
    title: str = ""
    reply: str = ""
    ask: str = ""
    step: str = ""
    agents: int = 0
    plan_done: int = 0
    plan_total: int = 0

    def alert(self, status):
        alert = generic_alert(status)
        title = preview_text(self.title, 80)
        if status == "complete":
            body = first_line(self.reply, 240)
        elif status in ATTENTION:
            body = preview_text(self.ask, 240)
        else:
            body = ""
        if title:
            alert["title"] = title
        if body:
            alert["body"] = body
        return alert

    def to_json(self):
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, value):
        try:
            data = json.loads(value) if value else None
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in fields})


def generic_alert(status):
    return {"title": GENERIC_TITLE, "body": GENERIC_ALERT_BODIES[status]}


def generic_activity_line(status, profile):
    if status in TERMINAL:
        return LABELS[status]
    name = agent_name(profile)
    return f"{name} needs you" if status in ATTENTION else f"{name} is working"


def destination(subscription, scope, run_id):
    target = {"version": 1, "surface": scope.surface,
              "installation_id": subscription["installation_id"], "connection_id": subscription["connection_id"],
              "profile": scope.profile, "run_id": run_id}
    target["canonical_root_id" if scope.surface == "native" else "session_id"] = scope.session_id
    return target


def alert_payload(subscription, run, scope, preview=None):
    alert = preview.alert(run["status"]) if subscription["preview_enabled"] and preview else generic_alert(run["status"])
    # thread-id is the opaque scope digest, so one chat stacks as one thread without naming it.
    aps = {"alert": alert, "sound": "default", "mutable-content": 1, "thread-id": scope.key}
    payload = {"aps": aps, "hermex.destination": destination(subscription, scope, run["run_id"]),
               "hermex.status": run["status"], "hermex.run_started_at": run["started_at"],
               "hermex.updated_at": run["updated_at"]}
    if (ident := agent_id(scope.profile)) is not None:
        payload["hermex.agent_id"] = ident
    if run["status"] in INPUT_KINDS:
        payload["hermex.input_kind"] = INPUT_KINDS[run["status"]]
    return payload


def content_state(run, scope, *, preview=None, previews=False):
    """Content-state v1 keys (decodable by every shipped app) plus optional v2 keys."""
    status = run["status"]
    final = status in TERMINAL
    preview = preview or AlertPreview()
    content = {"sessionID": scope.session_id, "sessionTitle": GENERIC_TITLE,
        "status": status, "currentActivity": generic_activity_line(status, scope.profile), "responseExcerpt": "",
        "startedAt": run["started_at"] - SWIFT_EPOCH, "updatedAt": run["updated_at"] - SWIFT_EPOCH,
        "isStale": False, "isFinal": final,
        "contentVersion": CONTENT_VERSION, "previewsEnabled": bool(previews)}
    if (ident := agent_id(scope.profile)) is not None:
        content["agentID"] = ident
    if status in INPUT_KINDS:
        content["inputKind"] = INPUT_KINDS[status]
    if not final and (agents := _count(preview.agents)):
        content["agentCount"] = agents
    if (total := _count(preview.plan_total)):
        content["planTotal"] = total
        content["planCompleted"] = min(_count(preview.plan_done), total)
    if previews:
        if title := preview_text(preview.title, 80):
            content["sessionTitle"] = title
        if status in ATTENTION and (ask := preview_text(preview.ask, 64)):
            content["currentActivity"] = ask
        elif not final and (step := preview_text(preview.step, 40)):
            content["currentStep"] = step
            content["currentActivity"] = step
        if status == "complete":
            content["responseExcerpt"] = first_line(preview.reply, 140)
    if status == "failed":
        content["errorSummary"] = "Open the chat for details."
    if run.get("activity_expired"):
        content["currentActivity"] = "Open the chat for the latest"
    if run.get("closed_reason") in CLOSED_LINES:
        content["currentActivity"] = CLOSED_LINES[run["closed_reason"]]
    return content


def redact_content_state(content, profile):
    """Dispatch-time privacy: a device that turned previews off gets no title, step or excerpt."""
    if not isinstance(content, dict) or not content.get("previewsEnabled"):
        return
    status = content.get("status")
    content.update(sessionTitle=GENERIC_TITLE, responseExcerpt="", previewsEnabled=False)
    content.pop("currentStep", None)
    if status in LABELS and content.get("currentActivity") not in CLOSED_LINES.values():
        content["currentActivity"] = generic_activity_line(status, profile)


def activity_payload(run, scope, now, *, preview=None, previews=False):
    final = run["status"] in TERMINAL
    aps = {"timestamp": int(run["updated_at"]), "event": "end" if final else "update",
           "content-state": content_state(run, scope, preview=preview, previews=previews),
           "relevance-score": 100 if run["status"] in ATTENTION else 0 if final else 10}
    aps["dismissal-date" if final else "stale-date"] = int(now + (300 if final else 120))
    return {"aps": aps}


def start_payload(subscription, run, scope, now, *, preview=None, previews=False):
    """Push-to-start (iOS 17.2+): attributes decode as AgentRunActivityAttributes."""
    preview = preview or AlertPreview()
    title = (preview_text(preview.title, 80) if previews else "") or GENERIC_TITLE
    attributes = {"sessionID": scope.session_id, "sessionTitle": title,
                  "startedAt": run["started_at"] - SWIFT_EPOCH}
    attributes["nativeDestination" if scope.surface == "native" else "chatsDestination"] = destination(
        subscription, scope, run["run_id"])
    name = agent_name(scope.profile)
    alert = {"title": title if previews else name, "body": f"{name} is working"}
    aps = {"timestamp": int(now), "event": "start",
           "content-state": content_state(run, scope, preview=preview, previews=previews),
           "attributes-type": "AgentRunActivityAttributes", "attributes": attributes,
           "alert": alert, "relevance-score": 10, "stale-date": int(now + 120)}
    return {"aps": aps}


def redact_start(aps, profile):
    if not isinstance(aps, dict) or not (aps.get("content-state") or {}).get("previewsEnabled"):
        return
    redact_content_state(aps["content-state"], profile)
    if isinstance(aps.get("attributes"), dict):
        aps["attributes"]["sessionTitle"] = GENERIC_TITLE
    aps["alert"] = {"title": agent_name(profile), "body": f"{agent_name(profile)} is working"}
