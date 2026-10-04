"""Sanitized presentation only; native account_usage owns HTTP and parsing."""
import base64
import json
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


def _safe_entry_label(entry, index):
    # Saved labels can be arbitrary credential material. Emit a closed label,
    # never an email, token fragment or provider response.
    return f'Account {index}'


def _decode_jwt_claims_unverified(token):
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
