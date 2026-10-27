"""Private, profile-owned dispatch shape audit; no application-log propagation."""
import json
import logging
import os
import stat

from .mobile_push_provider import send_shape

logger = logging.getLogger(__name__)


def audit_send(store_path, job, outcome, sent_at, *, reason=""):
    path = store_path.parent / "push-shape-audit.jsonl"
    try:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        # A misplaced FIFO must not block the delivery worker before the type check.
        flags |= getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("push audit must be a regular file")
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(send_shape(job, outcome, sent_at, reason=reason), separators=(",", ":")) + "\n")
    except OSError as error:
        # Never fall back to a less-protected sink or retry an already accepted send.
        logger.warning("mobile push audit unavailable (%s)", type(error).__name__)
