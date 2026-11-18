"""The tab each browser_exec call leaves the agent on.

browser_vault fills over the per-task CDP supervisor, whose page session starts on whichever tab it
attached to first; in a shared browser (the user's real profile, several named sessions) that is rarely
the tab the agent is working in. Every browser_exec call therefore asks its harness, as the call ends,
which tab it is attached to (``browser_use_shot.record_driven_tab_at_exit``) and records it as the task's
page hint, which the supervisor's tab selection prefers by exact target id.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import tempfile
from typing import Iterator, Optional

from tools.browser_use_shot import DRIVEN_TAB_FILE_ENV

logger = logging.getLogger(__name__)


_MAX_BYTES = 64 * 1024


def _read(path: str) -> tuple[str, str]:
    """The recorded tab, read without following a link or blocking on anything but a small regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return "", ""
            raw = json.loads(f.read(_MAX_BYTES + 1)[:_MAX_BYTES].decode("utf-8-sig") or "null")
    except (OSError, ValueError):
        return "", ""  # the call timed out, never reached the daemon, or wrote nothing
    if not isinstance(raw, dict):
        return "", ""
    return str(raw.get("url") or ""), str(raw.get("targetId") or "")


@contextlib.contextmanager
def recording(env: dict, task_id: Optional[str]) -> Iterator[None]:
    """Around one harness run: point the harness at a fresh file, then record what it wrote as
    ``task_id``'s driven tab. A call that leaves nothing behind keeps the previous hint."""
    fd, path = tempfile.mkstemp(prefix="hermes-driven-tab-", suffix=".json")
    os.close(fd)
    env[DRIVEN_TAB_FILE_ENV] = path
    try:
        yield
    finally:
        env.pop(DRIVEN_TAB_FILE_ENV, None)
        url, target_id = _read(path)
        with contextlib.suppress(OSError):
            os.unlink(path)
        if url and target_id:
            from tools.browser_supervisor import SUPERVISOR_REGISTRY
            SUPERVISOR_REGISTRY.note_page(task_id or "default", url, target_id)
