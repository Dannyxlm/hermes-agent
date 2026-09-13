"""Scoped alert projections; reply text is included only for opted-in devices."""

import hashlib
import re
import unicodedata
from dataclasses import dataclass

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


@dataclass(frozen=True)
class Scope:
    surface: str
    profile: str
    session_id: str

    def __post_init__(self):
        if self.surface not in {"native", "chats"}:
            raise ValueError("invalid surface")
        for value in (self.profile, self.session_id):
            if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
                raise ValueError("invalid canonical scope")

    @property
    def key(self):
        return hashlib.sha256("\0".join((self.surface, self.profile, self.session_id)).encode()).hexdigest()


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


@dataclass(frozen=True)
class AlertPreview:
    title: str = ""
    reply: str = ""

    def alert(self, status):
        alert = generic_alert(status)
        title = preview_text(self.title, 80)
        reply = preview_text(self.reply, 240) if status == "complete" else ""
        if title:
            alert["title"] = title
        if reply:
            alert["body"] = reply
        return alert


def generic_alert(status):
    text = {
        "complete": "A reply is ready. Open the app to view it.",
        "failed": "A run needs attention. Open the app for details.",
        "cancelled": "The run has stopped.",
        "waitingForApproval": "A run needs your approval. Open the app to review it.",
        "waitingForClarification": "A run needs your answer. Open the app to respond.",
    }[status]
    return {"title": "Hermex Ava", "body": text}


def alert_payload(subscription, run, scope, preview=None):
    destination = {"version": 1, "surface": scope.surface,
        "installation_id": subscription["installation_id"], "connection_id": subscription["connection_id"],
        "profile": scope.profile, "run_id": run["run_id"]}
    destination["canonical_root_id" if scope.surface == "native" else "session_id"] = scope.session_id
    alert = preview.alert(run["status"]) if subscription["preview_enabled"] and preview else generic_alert(run["status"])
    return {"aps": {"alert": alert, "sound": "default", "mutable-content": 1},
            "hermex.destination": destination, "hermex.status": run["status"],
            "hermex.run_started_at": run["started_at"], "hermex.updated_at": run["updated_at"]}


def activity_payload(run, scope, now):
    final = run["status"] in TERMINAL
    content = {"sessionID": scope.session_id, "sessionTitle": "Hermex Ava",
        "status": run["status"], "currentActivity": LABELS[run["status"]], "responseExcerpt": "",
        "startedAt": run["started_at"] - SWIFT_EPOCH, "updatedAt": run["updated_at"] - SWIFT_EPOCH,
        "isStale": False, "isFinal": final}
    if run["status"] == "failed":
        content["errorSummary"] = "Open the app for details."
    if run.get("activity_expired"):
        content["currentActivity"] = "Open app for current status"
    aps = {"timestamp": int(run["updated_at"]), "event": "end" if final else "update",
           "content-state": content}
    aps["dismissal-date" if final else "stale-date"] = int(now + (300 if final else 120))
    return {"aps": aps}
