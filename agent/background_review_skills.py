"""Session skill activity for the background review prompt.

A review fork inherits the parent's cached system prompt byte-for-byte (prompt-cache parity), so its
skill index is frozen at session start, and earlier review forks leave no trace in the transcript it
replays. Without this block, every review in a long conversation believes the skills its siblings
created do not exist yet. The block rides on the review's user message, after the cached prefix.
"""

from typing import Any, Dict, Iterable, List, Optional

_MAX_SKILLS = 12
_WRITE_ACTIONS = frozenset({"create", "edit", "patch", "write_file", "remove_file", "delete", "archive"})
_ACTOR_LABELS = {"curator": "a background review", "agent": "the conversation", "user": "the user"}


def _session_rows(session_ids: Iterable[Optional[str]]) -> List[Dict[str, Any]]:
    from tools import skill_ledger

    wanted = {sid for sid in session_ids if sid}
    if not wanted:
        return []
    rows = []
    for row in skill_ledger.list_entries():  # newest first
        evidence = row.get("evidence")
        if (isinstance(evidence, dict) and evidence.get("session_id") in wanted
                and row.get("action") in _WRITE_ACTIONS and row.get("skill")):
            rows.append(row)
    return rows


def session_skill_activity_block(session_ids: Iterable[Optional[str]]) -> str:
    """Prompt block listing skills written in this conversation (newest first), or ``""``."""
    from tools.skill_manager_tool import _find_skill

    touched: Dict[str, Dict[str, set]] = {}
    for row in _session_rows(session_ids):
        name = row["skill"]
        if name not in touched:
            if len(touched) >= _MAX_SKILLS:
                continue
            touched[name] = {"actions": set(), "actors": set()}
        touched[name]["actions"].add(row["action"])
        touched[name]["actors"].add(_ACTOR_LABELS.get(str(row.get("actor")), "the conversation"))
    if not touched:
        return ""
    lines = []
    for name, info in touched.items():
        verb = "created" if "create" in info["actions"] else "updated"
        status = "" if _find_skill(name) else " (no longer active)"
        lines.append(f"  • {name}{status}: {verb} by {' and '.join(sorted(info['actors']))}")
    return (
        "\n\nSkills already written in this conversation (from the skill ledger; the skill index in "
        "your system prompt predates some of them):\n" + "\n".join(lines) + "\n"
        "Check these first. If the learning fits one of them, skill_view it and patch it rather than "
        "creating a sibling skill.")
