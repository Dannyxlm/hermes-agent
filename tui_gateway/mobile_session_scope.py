"""Stored ordinary-session destinations; canonical Bot Chat authority stays separate."""
from pathlib import Path

from hermes_state import SessionDB
from .mobile_push_payloads import Scope


def resolve_destination(profile, home, stored_id):
    """Exact ID only, profile DB only, compression continuations only (never branches)."""
    path = Path(home) / "state.db"
    if not path.is_file():
        raise FileNotFoundError("profile state unavailable")
    with SessionDB(db_path=path, read_only=True) as db:
        row = db.get_session(stored_id)
        if not row:
            raise ValueError("unknown stored session")
        chain = db.get_compression_chain(stored_id)
        if not chain:
            raise ValueError("unknown lineage")
        root, tip = db.get_session(chain[0]), db.get_session(chain[-1])
        if not root or not tip or any(r.get("title") == SessionDB.CANONICAL_BOT_CHAT_TITLE for r in (root, tip)):
            raise ValueError("Bot Chat requires canonical registration")
        return Scope("native_session", profile, root["id"]), tip["id"], tip.get("title") or "Hermex Ava"
