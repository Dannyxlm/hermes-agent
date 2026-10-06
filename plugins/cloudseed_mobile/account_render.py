"""Sanitized presentation only; native account_usage owns HTTP and parsing."""
import base64
import json
import re
from datetime import datetime, timedelta, timezone
from agent.account_usage import _parse_dt


def _until(entry):
    if entry.last_status != 'exhausted':
        return None
    reset = _parse_dt(getattr(entry, 'last_error_reset_at', None))
    when = _parse_dt(getattr(entry, 'last_status_at', None))
    return reset or (when + timedelta(seconds=300 if str(entry.last_error_code) == '401' else 3600) if when else None)


def _entry_is_pool_exhausted(entry):
    until = _until(entry)
    return until is not None and datetime.now(timezone.utc) < until


def _entry_pool_retry_after(entry):
    until = _until(entry)
    return until.isoformat().replace('+00:00', 'Z') if until else None


def _valid_display_label(value):
    """A saved key name such as "DANNY-ANT": short, plain, never an email or token."""
    if not isinstance(value, str) or not value.strip():
        return False
    if (len(value) > 40
            or re.search(r'sk-|Bearer|@|[\x00-\x1f\x7f-\x9f]|[A-Za-z0-9+/=_\-]{20,}', value, re.IGNORECASE)):
        return False
    return re.fullmatch(r'[A-Za-z0-9 ._()\-]+', value) is not None


def _safe_entry_label(entry, index):
    # Show only the owner's saved key name. Saved labels are untrusted: emails,
    # token fragments and provider errors fall back to a closed label. Token
    # claims are never read for display (owner decision 2026-10-05: key names only).
    label = getattr(entry, 'label', None)
    return label if _valid_display_label(label) else f'Account {index}'


def _decode_jwt_claims_unverified(token):
    if not isinstance(token, str):
        return {}
    try:
        middle = token.split('.')[1]
        data = json.loads(base64.urlsafe_b64decode(middle + '=' * (-len(middle) % 4)))
        return data if isinstance(data, dict) else {}
    except (ValueError, IndexError, UnicodeError):
        return {}


def _serialize_account_usage_snapshot(snapshot):
    return {'windows': [
        {'label': w.label, 'used_percent': w.used_percent,
         'remaining_percent': max(0, min(100, 100-w.used_percent)) if w.used_percent is not None else None,
         'reset_at': w.reset_at.isoformat().replace('+00:00', 'Z') if w.reset_at else None,
         'detail': w.detail}
        for w in snapshot.windows
    ]}
