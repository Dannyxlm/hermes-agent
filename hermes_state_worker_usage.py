"""Closed validation for worker usage deltas and per-call ledger identity."""
from hermes_state_runtime import RuntimeStoreError
from hermes_state_usage import _MODEL_USAGE_FIELDS, _TOKEN_COUNTERS

_LEDGER_FIELDS = {"usage_timestamp", "turn_id", "usage_events"}


def _valid_usage_value(key, value):
    if key in (*_TOKEN_COUNTERS, "api_call_count"):
        return type(value) is int and 0 <= value <= 2**53
    if key in ("estimated_cost_usd", "actual_cost_usd"):
        return value is None or (type(value) in (int, float) and 0 <= value <= 1e12)
    if key == "usage_timestamp":
        return value is None or (type(value) in (int, float) and 0 <= value <= 2**53)
    if key == "absolute":
        return type(value) is bool
    return value is None or (isinstance(value, str) and len(value) <= 4096)


def _valid_events(events):
    if events is None:
        return True
    if not isinstance(events, list) or len(events) > 10000:
        return False
    allowed = _MODEL_USAGE_FIELDS | {"usage_timestamp", "turn_id", "source", "task", "absolute", "pricing_version"}
    return all(isinstance(event, dict) and not event.keys() - allowed
               and all(_valid_usage_value(k, v) for k, v in event.items()) for event in events)


def validate_usage(payload, *, auxiliary):
    allowed = (_MODEL_USAGE_FIELDS - {"billing_mode", "actual_cost_usd", "cost_status", "cost_source"}
               if auxiliary else _MODEL_USAGE_FIELDS | {"pricing_version", "absolute"}) | {"task"} | _LEDGER_FIELDS
    if payload.keys() - allowed:
        raise RuntimeStoreError("invalid_params")
    for key, value in payload.items():
        valid = _valid_events(value) if key == "usage_events" else _valid_usage_value(key, value)
        if not valid:
            raise RuntimeStoreError("invalid_params")
