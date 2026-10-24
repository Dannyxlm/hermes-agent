"""Launch observations cannot participate in the work's success or failure."""
import logging
import threading
import time
from contextlib import contextmanager
from functools import wraps

logger = logging.getLogger(__name__)
_warnings = {}
_warning_lock = threading.Lock()


def warning_due(operation):
    now = time.monotonic()
    with _warning_lock:
        if now - _warnings.get(operation, float('-inf')) < 60:
            return False
        _warnings[operation] = now
    return True


def warn(operation, exc):
    if warning_due(operation):
        logger.warning('Optional launch metadata skipped (%s): %s', operation, type(exc).__name__, exc_info=True)

def log_lock_holders(db_path, patience_s):
    if patience_s:
        from hermes_state_lockowners import log_write_lock_holders
        log_write_lock_holders(db_path, patience_s)


@contextmanager
def optional_launch(operation):
    try:
        yield
    except Exception as exc:
        if warning_due(operation):
            logger.warning('Optional launch metadata skipped (%s): %s', operation, type(exc).__name__, exc_info=True)


def best_effort(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with optional_launch(fn.__qualname__):
            return fn(*args, **kwargs)
        return None
    return wrapped
