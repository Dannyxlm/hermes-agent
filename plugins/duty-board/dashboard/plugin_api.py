"""Thin FastAPI routes; collectors also work in the gateway without FastAPI."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter
from pydantic import BaseModel

# The dashboard imports this file without a package. Use its sibling's path,
# not cwd/sys.path, so an installed plugin cannot accidentally import another.
_spec = importlib.util.spec_from_file_location(__name__ + "_board", Path(__file__).with_name("board.py"))
assert _spec and _spec.loader
board = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = board
_spec.loader.exec_module(board)
try:
    from hermes_constants import get_hermes_home
    board.get_hermes_home = get_hermes_home
except ImportError:  # outside Hermes
    pass
try:
    from agent.secret_scope import get_secret
    board.get_secret = get_secret
except ImportError:  # outside Hermes, or a host without profile-scoped secrets
    pass
router = APIRouter()
_cache = board._cache


@router.get("/summary")
def summary() -> Dict[str, Any]:
    return board.cached_summary()


class DismissBody(BaseModel):
    key: str
    undo: bool = False


_DISMISS_LOCK = threading.Lock()
_DISMISS_MAX_ENTRIES = 2000


@router.post("/dismiss")
def dismiss(body: DismissBody) -> Dict[str, Any]:
    key = (body.key or "").strip()
    if not key or len(key) > 200:
        return {"ok": False, "error": "bad key"}
    path = board._state_dir() / "dismissed.json"
    with _DISMISS_LOCK:
        data = board._dismissed()
        if body.undo:
            data.pop(key, None)
        else:
            data[key] = time.time()
        cutoff = time.time() - 30 * 86400
        fresh = [(k, value) for k, value in data.items() if isinstance(value, (int, float)) and value > cutoff]
        fresh.sort(key=lambda pair: pair[1], reverse=True)
        data = dict(fresh[:_DISMISS_MAX_ENTRIES])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".dismissed.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
        os.replace(tmp, path)
    with board._cache_lock:
        _cache.update(at=0.0, data=None, home=None)
    return {"ok": True}
