"""Resumable large-media upload slots (Hermex video sharing).

Videos are too big for the base64-over-WS attachment contract (``prompt_attachments``: 25 MB, and
base64 inflates it by a third inside one JSON frame). Instead a client:

1. mints a slot over the session-admitted WS RPC ``media.upload.begin`` (session + caps checked
   there, so only a caller that already owns the live session can create one);
2. streams the bytes to ``PUT /api/media/uploads/{upload_id}?offset=N`` in chunks; a dropped chunk
   resumes from ``GET /api/media/uploads/{upload_id}`` (the server's byte count is the truth);
3. finalizes with ``video.attach`` on the same session, which claims the slot (one use) and moves
   the bytes into that session's attachments.

Slots live in this process (the dashboard hosts both ``/api/ws`` and the REST routes); the bytes
live under ``<hermes home>/cache/media-uploads``. A restart drops the slots and the client starts
over with a fresh ``media.upload.begin``. Nothing here is profile config or transcript state.

Plain module (not ``bind_module``-rebound): import it inside handler bodies.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

VIDEO_MAX_BYTES = 300 * 1024 * 1024
VIDEO_MAX_SECONDS = 15 * 60
CHUNK_BYTES = 4 * 1024 * 1024
# A chunk PUT may carry at most this much (nginx's /api/ ingress caps bodies at 25 MB).
MAX_CHUNK_BYTES = 16 * 1024 * 1024
SLOT_IDLE_TTL_SECONDS = 60 * 60
MAX_OPEN_SLOTS = 8

VIDEO_EXTENSIONS: dict[str, str] = {
    ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mov": "video/quicktime", ".webm": "video/webm",
}

_UPLOAD_ID_ALPHABET = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


class UploadError(Exception):
    """Base class; ``status`` is the HTTP status the REST route answers with."""

    status = 400

    def __init__(self, message: str, **detail):
        super().__init__(message)
        self.detail = detail


class UploadNotFound(UploadError):
    status = 404


class UploadOffsetMismatch(UploadError):
    status = 409


class UploadTooLarge(UploadError):
    status = 413


class UploadConflict(UploadError):
    status = 409


class UploadRejected(UploadError):
    status = 422


@dataclass
class UploadSlot:
    upload_id: str
    session_key: str
    profile_home: str
    filename: str
    mime: str
    expected_bytes: int
    part_path: Path
    created_at: float
    touched_at: float
    received: int = 0
    claimed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def complete(self) -> bool:
        return self.received == self.expected_bytes

    def public(self) -> dict:
        return {"upload_id": self.upload_id, "offset": self.received, "size": self.expected_bytes,
                "complete": self.complete}


def valid_upload_id(value: object) -> bool:
    return isinstance(value, str) and 20 <= len(value) <= 64 and set(value) <= _UPLOAD_ID_ALPHABET


def video_mime_for(filename: str) -> str | None:
    return VIDEO_EXTENSIONS.get(Path(filename).suffix.lower())


def sniff_video_container(head: bytes) -> str | None:
    """``"mp4"`` (ISO BMFF: mp4/mov/m4v share ``ftyp``), ``"webm"`` (EBML) or None."""
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mp4"
    # QuickTime files written without a leading ftyp still start with a known atom.
    if len(head) >= 8 and head[4:8] in (b"moov", b"mdat", b"wide", b"free", b"skip"):
        return "mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    return None


class UploadRegistry:
    """Thread-safe slot table. ``root`` holds the in-progress ``.part`` files."""

    def __init__(self, root: Path | None = None, *, clock=time.time,
                 max_bytes: int = VIDEO_MAX_BYTES, idle_ttl: float = SLOT_IDLE_TTL_SECONDS,
                 max_open: int = MAX_OPEN_SLOTS):
        self._root = root
        self._clock = clock
        self.max_bytes = max_bytes
        self.idle_ttl = idle_ttl
        self.max_open = max_open
        self._slots: dict[str, UploadSlot] = {}
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        if self._root is None:
            from hermes_constants import get_hermes_home
            self._root = Path(get_hermes_home()) / "cache" / "media-uploads"
        return self._root

    # ── lifecycle ──────────────────────────────────────────────────────────

    def begin(self, *, session_key: str, profile_home: str, filename: str, size: int,
              mime: str | None = None) -> UploadSlot:
        if not session_key:
            raise UploadRejected("session has no durable key yet; send a message first")
        expected_mime = video_mime_for(filename)
        if expected_mime is None:
            raise UploadRejected(
                f"unsupported video type; supported: {', '.join(sorted(VIDEO_EXTENSIONS))}")
        if mime and not str(mime).lower().startswith("video/"):
            raise UploadRejected("mime must be a video type")
        if type(size) is not int or size <= 0:
            raise UploadRejected("size must be a positive integer byte count")
        if size > self.max_bytes:
            raise UploadTooLarge(
                f"video is {size // (1024 * 1024)} MB; the cap is {self.max_bytes // (1024 * 1024)} MB",
                max_bytes=self.max_bytes)
        self.sweep()
        root = self.root
        root.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(root, 0o700)
        upload_id = secrets.token_urlsafe(24)
        part = root / f"{upload_id}.part"
        fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        now = self._clock()
        slot = UploadSlot(upload_id=upload_id, session_key=session_key, profile_home=profile_home,
                          filename=filename, mime=expected_mime, expected_bytes=size, part_path=part,
                          created_at=now, touched_at=now)
        with self._lock:
            open_for_session = [s for s in self._slots.values()
                                if s.session_key == session_key and not s.claimed]
            if len(open_for_session) >= self.max_open:
                part.unlink(missing_ok=True)
                raise UploadConflict("too many unfinished uploads for this chat; cancel one first")
            self._slots[upload_id] = slot
        return slot

    def get(self, upload_id: str) -> UploadSlot:
        if not valid_upload_id(upload_id):
            raise UploadNotFound("unknown upload")
        with self._lock:
            slot = self._slots.get(upload_id)
        if slot is None or slot.claimed:
            raise UploadNotFound("unknown upload")
        if self._expired(slot):
            self.cancel(upload_id)
            raise UploadNotFound("upload expired; start again")
        return slot

    def status(self, upload_id: str) -> dict:
        return self.get(upload_id).public()

    def append(self, upload_id: str, offset: int, chunks: Iterable[bytes]) -> dict:
        """Write ``chunks`` at ``offset`` (must equal the bytes already received). A partial write
        (client hung up mid-chunk) keeps what arrived: the next status read reports it."""
        slot = self.get(upload_id)
        if not slot.lock.acquire(blocking=False):
            raise UploadConflict("another chunk for this upload is still being written")
        try:
            if type(offset) is not int or offset != slot.received:
                raise UploadOffsetMismatch(
                    f"offset {offset} does not match the {slot.received} bytes already received",
                    offset=slot.received)
            written = 0
            with open(slot.part_path, "r+b") as fh:
                fh.seek(slot.received)
                try:
                    for chunk in chunks:
                        if not chunk:
                            continue
                        if written + len(chunk) > MAX_CHUNK_BYTES:
                            raise UploadTooLarge("chunk too large", max_chunk_bytes=MAX_CHUNK_BYTES)
                        if slot.received + len(chunk) > slot.expected_bytes:
                            raise UploadTooLarge("more bytes than the declared size",
                                                 size=slot.expected_bytes)
                        fh.write(chunk)
                        written += len(chunk)
                        slot.received += len(chunk)
                finally:
                    fh.flush()
                    fh.truncate(slot.received)
            slot.touched_at = self._clock()
            return slot.public()
        finally:
            slot.lock.release()

    def cancel(self, upload_id: str) -> bool:
        if not valid_upload_id(upload_id):
            return False
        with self._lock:
            slot = self._slots.pop(upload_id, None)
        if slot is None:
            return False
        slot.part_path.unlink(missing_ok=True)
        return True

    def claim(self, upload_id: str, *, session_key: str) -> UploadSlot:
        """One-use hand-off to ``video.attach``: the slot must belong to this session and be complete.
        The caller owns ``slot.part_path`` afterwards (moves or deletes it)."""
        slot = self.get(upload_id)
        if slot.session_key != session_key:
            # Same answer as an unknown id: never confirm another chat's upload exists.
            raise UploadNotFound("unknown upload")
        if not slot.lock.acquire(blocking=False):
            raise UploadConflict("upload is still receiving bytes")
        try:
            if not slot.complete:
                raise UploadConflict(
                    f"upload incomplete ({slot.received} of {slot.expected_bytes} bytes)",
                    offset=slot.received)
            with open(slot.part_path, "rb") as fh:
                head = fh.read(16)
            if sniff_video_container(head) is None:
                self.cancel(upload_id)
                raise UploadRejected("the uploaded bytes are not a video file")
            with self._lock:
                slot.claimed = True
                self._slots.pop(upload_id, None)
            return slot
        finally:
            slot.lock.release()

    def sweep(self) -> int:
        """Drop idle slots (and orphaned ``.part`` files from a previous process)."""
        with self._lock:
            stale = [k for k, s in self._slots.items() if self._expired(s)]
            live_parts = {s.part_path.name for s in self._slots.values()}
        for key in stale:
            self.cancel(key)
        removed = len(stale)
        root = self._root
        if root is not None and root.is_dir():
            cutoff = self._clock() - self.idle_ttl
            for entry in root.glob("*.part"):
                if entry.name in live_parts:
                    continue
                try:
                    if entry.stat().st_mtime < cutoff:
                        entry.unlink(missing_ok=True)
                        removed += 1
                except OSError:
                    continue
        return removed

    def _expired(self, slot: UploadSlot) -> bool:
        return self._clock() - slot.touched_at > self.idle_ttl


_registry: Optional[UploadRegistry] = None
_registry_lock = threading.Lock()


def registry() -> UploadRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = UploadRegistry()
        return _registry


def set_registry_for_tests(value: Optional[UploadRegistry]) -> None:
    global _registry
    with _registry_lock:
        _registry = value


def limits() -> dict:
    """Client-facing caps, advertised through ``mobile.capabilities``."""
    return {"max_bytes": VIDEO_MAX_BYTES, "max_seconds": VIDEO_MAX_SECONDS,
            "chunk_bytes": CHUNK_BYTES, "extensions": sorted(VIDEO_EXTENSIONS)}
