"""Bounded readers for the CloudSeed managed-update status payload (overlay ``managed-upstream-status``).

Pure helpers moved out of ``hermes_cli/web_server.py`` unchanged, so the managed-update overlay carries
fewer lines inside the upstream-owned facade. ``web_server`` re-imports them under the same names.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

_MANAGED_UPDATE_MAX_BYTES = 64 * 1024


def _managed_safe_string(
    value: Any, *, maximum: int, allow_empty: bool = False
) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if (not text and not allow_empty) or len(text) > maximum:
        return None
    if any(ord(ch) < 32 and ch not in "\t" for ch in text):
        return None
    return text


def _managed_timestamp(value: Any) -> Optional[datetime]:
    text = _managed_safe_string(value, maximum=64)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _managed_read_bounded_json(path: Path) -> tuple[Optional[dict[str, Any]], str]:
    """Read one exact status file without following links or leaking content."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd: Optional[int] = None
    try:
        parent = path.parent
        if (
            not path.is_absolute()
            or parent.resolve(strict=True) != parent.absolute()
        ):
            return None, "status_path_unsafe"
        fd = os.open(str(path), flags)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "status_not_regular"
        if st.st_size > _MANAGED_UPDATE_MAX_BYTES:
            return None, "status_too_large"
        if st.st_mode & 0o022:
            return None, "status_permissions_unsafe"
        if hasattr(os, "geteuid") and st.st_uid not in {0, os.geteuid()}:
            return None, "status_owner_invalid"

        chunks: list[bytes] = []
        total = 0
        while total <= _MANAGED_UPDATE_MAX_BYTES:
            chunk = os.read(fd, min(8192, _MANAGED_UPDATE_MAX_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > _MANAGED_UPDATE_MAX_BYTES:
            return None, "status_too_large"
        try:
            decoded = b"".join(chunks).decode("utf-8")
            payload = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "status_json_invalid"
        if not isinstance(payload, dict):
            return None, "status_schema_invalid"
        return payload, ""
    except FileNotFoundError:
        return None, "status_missing"
    except (OSError, RuntimeError):
        return None, "status_unreadable"
    finally:
        if fd is not None:
            os.close(fd)


def _managed_nonnegative_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > 1_000_000:
        return None
    return value
