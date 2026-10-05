"""Private, immutable recovery results scoped to one attached runtime and peer.

Only opted-in recovery reads use this spool. Handles grant no authority by
themselves, and capacity failures never return a partial successful snapshot.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import secrets
import tempfile
import threading
import time
from typing import BinaryIO

from agent.message_sanitization import _sanitize_surrogates

logger = logging.getLogger(__name__)

INLINE_BYTES = 1024 * 1024
CHUNK_BYTES = 192 * 1024
MAX_TRANSFER_BYTES = 64 * 1024 * 1024
MAX_PROCESS_BYTES = 256 * 1024 * 1024
MAX_TRANSFERS = 32
MAX_PEER_TRANSFERS = 2
EXPIRY_SECONDS = 120


class TransferError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def attached_session(sid, profile, transport):
    from . import server
    session = server._sessions.get(sid)
    if (not session or session.get("_finalized")
            or not server._session_transport_contains(session, transport)):
        raise TransferError(4400, "session must be attached to this connection")
    home = Path(session.get("profile_home") or server._hermes_home).resolve()
    if profile is not None:
        from hermes_cli.profiles import get_profile_dir, validate_profile_name
        try:
            validate_profile_name(profile)
            expected = Path(get_profile_dir(profile)).resolve()
        except (ValueError, TypeError):
            raise TransferError(4400, "runtime profile mismatch") from None
        if expected != home:
            raise TransferError(4400, "runtime profile mismatch")
    return session, home


@dataclass(eq=False)
class _Preparation:
    transport: object
    session: dict | None = None
    size: int = 0
    revoked: bool = False


@dataclass
class _Transfer:
    sid: str
    session: dict
    home: Path
    transport: object
    file: BinaryIO
    size: int
    expires: float
    timer: threading.Timer | None = None


class ResponseTransfers:
    def __init__(self):
        self._lock = threading.RLock()
        self._entries: dict[str, _Transfer] = {}
        self._preparing: set[_Preparation] = set()

    def _drop(self, handle):
        entry = self._entries.pop(handle, None)
        if entry is not None:
            if entry.timer is not None:
                entry.timer.cancel()
            try:
                entry.file.close()
            except OSError as exc:
                logger.warning("recovery transfer spool close failed (%s)", type(exc).__name__)
        return entry is not None

    def _valid(self, entry):
        try:
            session, home = attached_session(entry.sid, None, entry.transport)
            return session is entry.session and home == entry.home and time.monotonic() < entry.expires
        except TransferError:
            return False

    def _prune(self):
        for handle, entry in list(self._entries.items()):
            if not self._valid(entry):
                self._drop(handle)

    def _expire(self, handle):
        with self._lock:
            entry = self._entries.get(handle)
            if entry is None:
                return
            remaining = entry.expires - time.monotonic()
            if remaining <= 0:
                self._drop(handle)
                return
            timer = threading.Timer(remaining, self._expire, args=(handle,))
            timer.daemon = True
            entry.timer = timer
            try:
                timer.start()
            except RuntimeError:
                self._drop(handle)

    def reserve(self, transport):
        """Admit a writer before resume side effects; encoding never holds this lock."""
        with self._lock:
            self._prune()
            published = len(self._entries)
            peer_published = sum(e.transport is transport for e in self._entries.values())
            if published >= MAX_TRANSFERS or peer_published >= MAX_PEER_TRANSFERS:
                raise TransferError(4413, "recovery transfer capacity exceeded; release transfers before retrying")
            if (published + len(self._preparing) >= MAX_TRANSFERS
                    or peer_published + sum(p.transport is transport for p in self._preparing) >= MAX_PEER_TRANSFERS):
                raise TransferError(4414, "recovery serialization busy; retry shortly")
            if sum(e.size for e in self._entries.values()) >= MAX_PROCESS_BYTES:
                raise TransferError(4413, "recovery transfer capacity exceeded; release transfers before retrying")
            reservation = _Preparation(transport)
            self._preparing.add(reservation)
            return reservation

    def cancel(self, reservation):
        with self._lock:
            self._preparing.discard(reservation)

    def _grow(self, reservation, count):
        with self._lock:
            if reservation.revoked:
                raise TransferError(4400, "connection detached during recovery")
            if reservation.size + count > MAX_TRANSFER_BYTES:
                raise TransferError(4413, "recovery response exceeds transfer capacity")
            stored = sum(e.size for e in self._entries.values())
            preparing = sum(p.size for p in self._preparing)
            if stored + preparing + count > MAX_PROCESS_BYTES:
                code = 4414 if preparing > reservation.size else 4413
                raise TransferError(code, "recovery serialization busy; retry shortly" if code == 4414
                                    else "recovery response exceeds transfer capacity")
            reservation.size += count

    def prepare(self, result, sid, profile, transport, *, reservation=None):
        """Serialize once privately, reserving bytes before each write, then publish atomically."""
        session, home = attached_session(sid, profile, transport)
        reservation = reservation or self.reserve(transport)
        spool = None
        try:
            with self._lock:
                if reservation.transport is not transport or reservation.revoked:
                    raise TransferError(4400, "connection detached during recovery")
                reservation.session = session
            spool = tempfile.TemporaryFile(mode="w+b", prefix="hermes-recovery-")
            for part in json.JSONEncoder(ensure_ascii=False).iterencode(result):
                # Match WebSocket text sanitation without changing source state.
                data = _sanitize_surrogates(part).encode("utf-8")
                self._grow(reservation, len(data))
                spool.write(data)
            with self._lock:
                current, current_home = attached_session(sid, profile, transport)
                if reservation.revoked or current is not session or current_home != home:
                    raise TransferError(4400, "runtime changed during recovery")
                size = reservation.size
                if size <= INLINE_BYTES:
                    return result
                handle = secrets.token_urlsafe(24)
                entry = _Transfer(sid, session, home, transport, spool, size,
                                  time.monotonic() + EXPIRY_SECONDS)
                timer = threading.Timer(EXPIRY_SECONDS, self._expire, args=(handle,))
                timer.daemon = True
                entry.timer = timer
                self._entries[handle] = entry
                self._preparing.discard(reservation)
                try:
                    timer.start()
                except RuntimeError:
                    self._drop(handle)
                    raise TransferError(4413, "recovery transfer expiry unavailable") from None
                spool = None  # registry owns the descriptor until release or expiry
                return {"response_transfer": {"transfer_id": handle, "session_id": sid,
                        "encoding": "base64-json-utf8", "byte_length": size,
                        "chunk_bytes": CHUNK_BYTES, "expires_in": EXPIRY_SECONDS}}
        except OSError:
            raise TransferError(4413, "recovery transfer storage unavailable") from None
        finally:
            self.cancel(reservation)
            if spool is not None:
                try:
                    spool.close()
                except OSError as exc:
                    logger.warning("recovery transfer spool close failed (%s)", type(exc).__name__)

    def _owned(self, handle, sid, profile, transport):
        self._prune()
        entry = self._entries.get(handle)
        session, home = attached_session(sid, profile, transport)
        if (entry is None or entry.transport is not transport or entry.sid != sid
                or entry.session is not session or entry.home != home):
            raise TransferError(4400, "recovery transfer unavailable for this connection")
        return entry

    def read(self, handle, sid, profile, transport, offset):
        with self._lock:
            entry = self._owned(handle, sid, profile, transport)
            if type(offset) is not int or not 0 <= offset < entry.size:
                raise TransferError(4000, "invalid recovery transfer offset")
            entry.file.seek(offset)
            data = entry.file.read(CHUNK_BYTES)
            next_offset = offset + len(data)
            entry.expires = time.monotonic() + EXPIRY_SECONDS
            return {"transfer_id": handle, "offset": offset,
                    "data": base64.b64encode(data).decode("ascii"),
                    "next_offset": next_offset, "eof": next_offset == entry.size}

    def release(self, handle, sid, profile, transport):
        with self._lock:
            self._owned(handle, sid, profile, transport)
            return {"released": self._drop(handle)}

    def forget(self, *, transport=None, session=None):
        with self._lock:
            for pending in self._preparing:
                if ((transport is not None and pending.transport is transport)
                        or (session is not None and pending.session is session)):
                    pending.revoked = True
            for handle, entry in list(self._entries.items()):
                if ((transport is not None and entry.transport is transport)
                        or (session is not None and entry.session is session)):
                    self._drop(handle)


transfers = ResponseTransfers()


def recovery_reply(rid, result, sid, params):
    from . import server
    if params.get("chunked_response") is not True:
        return server._ok(rid, result)
    try:
        return server._ok(rid, transfers.prepare(result, sid, params.get("profile"), server.current_transport()))
    except TransferError as exc:
        return server._err(rid, exc.code, str(exc))
    except OSError:
        return server._err(rid, 4413, "recovery transfer storage unavailable")


def transfer_rpc(rid, params, *, release=False):
    from . import server
    handle, sid = params.get("transfer_id"), params.get("session_id")
    if not isinstance(handle, str) or not isinstance(sid, str):
        return server._err(rid, 4000, "invalid recovery transfer identity")
    args = (handle, sid, params.get("profile"), server.current_transport())
    try:
        result = transfers.release(*args) if release else transfers.read(*args, params.get("offset"))
        return server._ok(rid, result)
    except TransferError as exc:
        return server._err(rid, exc.code, str(exc))
    except OSError:
        return server._err(rid, 4413, "recovery transfer storage unavailable")
