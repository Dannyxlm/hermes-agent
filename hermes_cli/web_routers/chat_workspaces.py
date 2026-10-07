"""Chat workspace picker: which host directories a NEW dashboard chat can start in.

The dashboard's ``/chat`` tab used to spawn every fresh TUI in the dashboard process's own
launch directory, so from a phone or browser there was no way to say "work in ~/code/foo".
This lists the same projects and discovered repositories the Desktop sidebar shows (the
per-profile ``projects.db`` plus session-derived and scanned git roots) so the SPA can offer
a picker, and ``/api/pty?cwd=`` (``chat_ws``) honours the choice.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

from hermes_cli.web_deps import late
from hermes_cli.web_routers._common import http_failure, scoped_to_thread

router = APIRouter()

_open_session_db_for_profile = late("_open_session_db_for_profile", "hermes_cli.web_server_sessions")


def resolve_chat_cwd(raw: Optional[str]) -> Optional[str]:
    """Validate a ``?cwd=`` for a fresh chat: an existing host directory, or None when unset.

    A dead or relative path fails closed (400) instead of silently falling back to the launch
    dir — the user picked a workspace and would otherwise get a session in the wrong place.
    """
    text = (raw or "").strip()
    if not text:
        return None
    resolved = os.path.abspath(os.path.expanduser(text))
    if not os.path.isdir(resolved):
        raise HTTPException(status_code=400, detail=f"Working directory does not exist: {text}")
    return resolved


_ALIAS_SCAN_LIMIT = 500


def _workspace_aliases(projects: list) -> dict:
    """Old folder names per project root, for client-side name search only.

    A workspace folder renamed to a short name often keeps its old long name as a symlink in the
    same parent (``CloudSeed Strategy -> CloudSeed``). Clients can't see symlink targets through
    ``fs/list``, so list them here: symlinks placed directly in the parent of a project folder
    whose ``realpath`` is that folder. Read-only, names only: no new paths, no grant change, no
    file contents. Unreadable parents are skipped; at most ``_ALIAS_SCAN_LIMIT`` entries are
    examined per parent.
    """
    roots: dict = {}
    for project in projects or []:
        if not isinstance(project, dict) or project.get("archived"):
            continue
        paths = [project.get("primary_path")] + [
            (folder or {}).get("path") for folder in (project.get("folders") or []) if isinstance(folder, dict)]
        for path in paths:
            if isinstance(path, str) and os.path.isabs(path):
                root = os.path.normpath(path)
                if root not in roots:
                    try:
                        roots[root] = os.path.realpath(root)
                    except OSError:
                        continue
    by_parent: dict = {}
    for root, real in roots.items():
        by_parent.setdefault(os.path.dirname(root), {}).setdefault(real, []).append(root)
    aliases: dict = {}
    for parent, targets in by_parent.items():
        try:
            with os.scandir(parent) as entries:
                for index, entry in enumerate(entries):
                    if index >= _ALIAS_SCAN_LIMIT:
                        break
                    try:
                        if not entry.is_symlink():
                            continue
                        target = os.path.realpath(entry.path)
                    except OSError:
                        continue
                    for root in targets.get(target, []):
                        if entry.name != os.path.basename(root):
                            aliases.setdefault(root, []).append(entry.name)
        except OSError:
            continue
    return {root: sorted(set(names)) for root, names in aliases.items()}


def _collect_workspaces(profile: Optional[str], scan: bool) -> dict:
    # The dashboard hosts the in-process gateway (``web_server`` imports ``tui_gateway.server``
    # at startup), so the sidebar's repo-discovery helpers are already bound there.
    import tui_gateway.server as gateway
    from hermes_cli import projects_db as pdb

    db = _open_session_db_for_profile(profile, read_only=True)
    try:
        with pdb.connect_closing() as conn:
            policy = gateway._repo_discovery_policy()
            if scan and policy["enabled"]:
                gateway._scan_discovered_repos_remote(conn, policy)
            projects = [p.to_dict() for p in pdb.list_projects(conn)]
            repos = gateway._discover_repos_payload(
                db, conn=conn, backfill=False, include_cached=policy["enabled"])
    finally:
        db.close()
    default_cwd = gateway._completion_cwd({"profile": profile} if profile else {})
    return {
        "projects": projects, "repos": repos, "default_cwd": default_cwd,
        "home": str(Path.home()), "scan_enabled": bool(policy["enabled"]),
        # Additive: old long folder names (symlinks) per project root, for name search only.
        "aliases": _workspace_aliases(projects)}


@router.get("/api/chat/workspaces")
async def get_chat_workspaces(profile: Optional[str] = None, scan: bool = False):
    """Projects + discovered repos a fresh chat may start in; ``scan=1`` rescans the
    configured discovery roots on the host first (headless installs have no Desktop to do it)."""
    with http_failure("GET /api/chat/workspaces failed", 500, "Failed to list workspaces"):
        return await scoped_to_thread(profile, lambda: _collect_workspaces(profile, scan))
