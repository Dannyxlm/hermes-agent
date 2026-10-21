"""Scoped review of committed ledger changes with compare-before-write Undo."""
from __future__ import annotations

from contextlib import contextmanager
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from hermes_constants import get_hermes_home
from hermex_live_reads import LiveConflict, cursor_decode, cursor_encode, digest, owned_lineage
from tools import skill_ledger
from tools.skill_usage import skill_file_lock


@contextmanager
def _review_lock():
    with skill_file_lock(get_hermes_home() / "skills" / ".locks" / "hermex-review.lock"):
        yield


def _journal_path():
    return get_hermes_home() / "skills" / ".hermex-review.json"


def _journal():
    path = _journal_path()
    return json.loads(path.read_text()) if path.exists() else {"actions": {}, "changes": {}}


def _sync_directory(path):
    if os.name == "posix":
        directory = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _durable_bytes(path, data):
    created = [parent for parent in path.parents if not parent.exists()]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".hermex-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
        for parent in created:
            _sync_directory(parent.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _save_journal(journal):
    _durable_bytes(_journal_path(), json.dumps(journal, sort_keys=True).encode())


def _state(journal, change_id):
    action_key = journal["changes"].get(change_id)
    action = journal["actions"].get(action_key) if action_key else None
    if not action or action["status"] != "complete":
        return "unreviewed", None
    return action["receipt"]["decision"], action["receipt"]["receipt_id"]


def _revision(entry, state):
    return digest([entry, state])


def _admitted_entry(db, profile, change_id):
    entry = skill_ledger.get_entry(change_id)
    if (entry is None or not (entry.get("before") or entry.get("after"))
            or entry.get("evidence", {}).get("review_committed") is not True
            or not entry.get("evidence", {}).get("session_id")):
        raise ValueError("change unavailable")
    owned_lineage(db, entry["evidence"]["session_id"], profile)
    return entry


def capture_is_complete(root, manifest):
    """A best-effort ledger snapshot cannot authorize deletion of an uncaptured old file."""
    if manifest is None:
        return False
    if root is None:
        return True
    recorded = {item["path"]: item["sha256"] for item in manifest}
    def fail(error):
        raise error
    try:
        for directory, dirs, files in os.walk(root, onerror=fail, followlinks=False):
            dirs[:] = [name for name in dirs if name not in skill_ledger.TRANSIENT_DIRS]
            if any((Path(directory) / name).is_symlink() for name in dirs):
                return False
            for name in files:
                path = Path(directory) / name
                if path.is_symlink() or recorded.get(str(path)) != hashlib.sha256(path.read_bytes()).hexdigest():
                    return False
        return True
    except OSError:
        return False


def _safe_manifest(entry):
    home = get_hermes_home()
    root = home / "skills"
    if root.is_symlink():
        raise LiveConflict("redirected skill root")
    manifests = []
    for section in ("before", "after"):
        files = {}
        for item in entry.get(section) or []:
            path = Path(item["path"])
            if not path.is_absolute() or not path.is_relative_to(root):
                raise LiveConflict("change is outside this profile's skills")
            parts = path.relative_to(root).parts
            if len(parts) < 2 or any(part.startswith(".") or part == ".." for part in parts):
                raise LiveConflict("unsafe change path")
            if any(parent.is_symlink() for parent in (path, *path.parents) if parent != home.parent):
                raise LiveConflict("redirected change path")
            sha = item["sha256"]
            if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None or path in files:
                raise LiveConflict("invalid change manifest")
            files[path] = sha
        manifests.append(files)
    if not any(manifests) or entry.get("evidence", {}).get("capture_complete") is not True:
        raise LiveConflict("complete change backup unavailable")
    return manifests


def _blob(sha):
    data = skill_ledger.read_blob(sha)
    if data is None or hashlib.sha256(data).hexdigest() != sha:
        raise LiveConflict("change backup unavailable")
    return data


def _current_hash(path):
    if not path.exists():
        return None
    if not path.is_file():
        raise LiveConflict("changed path is not a file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _undo_plan(entry, *, recovering=False):
    before, after = _safe_manifest(entry)
    contents = {path: _blob(sha) for path, sha in before.items()}
    for path in before.keys() | after.keys():
        current = _current_hash(path)
        if current != after.get(path) and not (recovering and current == before.get(path)):
            raise LiveConflict("skill changed since this edit; Undo refused")
    return before, after, contents


def _can_undo(entry, journal):
    if _state(journal, entry["id"])[0] == "undone":
        return False
    if any(a["status"] == "prepared" and a["change_id"] == entry["id"] for a in journal["actions"].values()):
        return False
    try:
        _undo_plan(entry)
        return True
    except (LiveConflict, OSError):
        return False


def _diff(entry):
    try:
        before, after = _safe_manifest(entry)
        diffs = []
        for path in sorted(before.keys() | after.keys()):
            left = _blob(before[path]).decode("utf-8").splitlines() if path in before else []
            right = _blob(after[path]).decode("utf-8").splitlines() if path in after else []
            relative = path.relative_to(get_hermes_home() / "skills").as_posix()
            diffs.extend(difflib.unified_diff(left, right, fromfile=relative, tofile=relative, lineterm=""))
        return "\n".join(diffs)
    except (LiveConflict, OSError, UnicodeError):
        return None


def _item(entry, journal):
    state, _receipt = _state(journal, entry["id"])
    diff = _diff(entry)
    lines = [{"kind": "add" if line.startswith("+") else "remove", "text": line[1:]}
             for line in (diff or "").splitlines()
             if (line.startswith("+") and not line.startswith("+++"))
             or (line.startswith("-") and not line.startswith("---"))]
    evidence = entry["evidence"]
    return {"change_id": entry["id"], "skill": entry["skill"], "session_id": evidence["session_id"],
            "anchor_message_id": evidence.get("anchor_message_id"), "created_at": entry["ts"],
            "revision": _revision(entry, state), "title": evidence.get("title"), "reason": evidence.get("reason"),
            "lines": lines, "decision": state, "can_undo": _can_undo(entry, journal)}


def list_changes(db, params, principal):
    chain = owned_lineage(db, params["session_id"], params["profile"])
    with _review_lock():
        journal = _journal()
        entries = [e for e in skill_ledger.list_entries()
                   if (e.get("before") or e.get("after"))
                   and e.get("evidence", {}).get("review_committed") is True
                   and e.get("evidence", {}).get("session_id") in chain]
        revision = digest([[e["id"], _state(journal, e["id"])] for e in entries])
        scope = ["changes", principal, params["profile"], str(get_hermes_home()), chain, revision]
        offset = cursor_decode(params["cursor"], scope) if params.get("cursor") else 0
        limit = params.get("limit", 20)
        items = [_item(e, journal) for e in entries[offset:offset + limit]]
        next_cursor = cursor_encode(scope, offset + limit) if offset + limit < len(entries) else None
    return {"version": 1, "profile": params["profile"], "items": items, "next_cursor": next_cursor}


def _change_lock_name(entry):
    for item in (entry.get("before") or []) + (entry.get("after") or []):
        path = Path(item["path"])
        if path.name == "SKILL.md":
            return path.parent.name
    return entry["skill"]


def get_change(db, params):
    entry = _admitted_entry(db, params["profile"], params["change_id"])
    from tools.skill_manager_tool import _skill_mutation_lock
    with _skill_mutation_lock(_change_lock_name(entry)), _review_lock():
        journal = _journal()
        pending = next((a for a in journal["actions"].values()
                        if a["change_id"] == entry["id"] and a["status"] == "prepared"), None)
        if pending is not None:
            if pending["payload"]["decision"] == "undo":
                _apply_undo(entry, recovering=True, action=pending)
            pending["status"] = "complete"
            key = next(k for k, action in journal["actions"].items() if action is pending)
            journal["changes"][entry["id"]] = key
            _save_journal(journal)
        state, receipt = _state(journal, entry["id"])
        return {"version": 1, "change_id": entry["id"], "revision": _revision(entry, state),
                "diff": _diff(entry), "can_undo": _can_undo(entry, journal), "decision": state, "receipt_id": receipt}


def _apply_undo(entry, recovering, action):
    before, after, contents = _undo_plan(entry, recovering=recovering)
    root = Path(action["undo_root"])
    for path, data in contents.items():
        if _current_hash(path) != before[path]:
            _durable_bytes(path, data)
    for path in after.keys() - before.keys():
        if path.exists():
            path.unlink()
            _sync_directory(path.parent)
    from agent.prompt_builder import clear_skills_system_prompt_cache
    clear_skills_system_prompt_cache(clear_snapshot=True)
    receipt = action["receipt"]["receipt_id"]
    if any(e.get("evidence", {}).get("undo_receipt") == receipt for e in skill_ledger.list_entries()):
        return
    audit_id = skill_ledger.record_mutation(
        "undo", entry["skill"], before=action["undo_before"], after_root=root if root.exists() else None,
        evidence={"undo_of": entry["id"], "undo_receipt": receipt,
                  "session_id": entry["evidence"]["session_id"], "review_committed": False})
    if audit_id is None and skill_ledger.ledger_enabled():
        raise OSError("Undo audit write unavailable; recover the prepared receipt")
    from tools import skill_usage
    if entry["action"] == "create":
        skill_usage.forget(entry["skill"])
    else:
        skill_usage.bump_patch(entry["skill"], session_id=entry["evidence"]["session_id"])


def decide_change(db, params, principal):
    entry = _admitted_entry(db, params["profile"], params["change_id"])
    from tools.skill_manager_tool import _skill_mutation_lock
    key = digest([principal, params["client_action_id"]])
    payload = {k: params[k] for k in ("change_id", "decision", "expected_revision", "client_action_id")}
    with _skill_mutation_lock(_change_lock_name(entry)), _review_lock():
        journal = _journal()
        action = journal["actions"].get(key)
        if action is not None:
            if action["payload"] != payload:
                raise LiveConflict("client_action_id already used for another decision")
            if action["status"] == "complete":
                return action["receipt"]
        else:
            state, _receipt = _state(journal, entry["id"])
            if params["expected_revision"] != _revision(entry, state) or state == "undone":
                raise LiveConflict("change review revision changed")
            if any(a["status"] == "prepared" and a["change_id"] == entry["id"] for a in journal["actions"].values()):
                raise LiveConflict("an earlier decision needs acknowledgement recovery")
            if params["decision"] == "undo":
                _undo_plan(entry)
            decision = "undone" if params["decision"] == "undo" else "kept"
            action = {"payload": payload, "change_id": entry["id"], "status": "prepared",
                      "receipt": {"change_id": entry["id"], "decision": decision,
                                  "revision": _revision(entry, decision), "receipt_id": uuid.uuid4().hex}}
            if params["decision"] == "undo":
                paths = [Path(item["path"]).parent for item in (entry.get("before") or []) + (entry.get("after") or [])]
                root = Path(os.path.commonpath(paths))
                action["undo_root"] = str(root)
                action["undo_before"] = skill_ledger.capture_before(root)
            journal["actions"][key] = action
            _save_journal(journal)
        if params["decision"] == "undo":
            _apply_undo(entry, recovering=True, action=action)
        action["status"] = "complete"
        journal["changes"][entry["id"]] = key
        _save_journal(journal)
        return action["receipt"]
