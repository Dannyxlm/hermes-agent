from __future__ import annotations

import threading
import time

from plugins.memory.honcho.bounds import (
    cap_records,
    cap_string_list,
    cap_text,
    run_bounded,
)


def test_bounded_call_invokes_once_and_returns_typed_success():
    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        return "ok"

    outcome = run_bounded(operation, timeout_seconds=0.2)
    assert outcome.status == "ok"
    assert outcome.value == "ok"
    assert outcome.error_code == ""
    assert calls == 1


def test_timeout_returns_without_waiting_and_late_result_cannot_contaminate_next_call():
    release = threading.Event()
    calls = 0

    def slow():
        nonlocal calls
        calls += 1
        release.wait(timeout=1)
        return "late-private-result"

    started = time.monotonic()
    timed_out = run_bounded(slow, timeout_seconds=0.02)
    elapsed = time.monotonic() - started
    assert timed_out.status == "degraded"
    assert timed_out.error_code == "deadline_exceeded"
    assert timed_out.value is None
    assert elapsed < 0.2
    assert calls == 1

    fresh = run_bounded(lambda: "fresh", timeout_seconds=0.2)
    assert fresh.value == "fresh"
    release.set()
    time.sleep(0.02)
    assert fresh.value == "fresh"


def test_exception_is_sanitized_without_retry_or_message_leak():
    calls = 0

    def broken():
        nonlocal calls
        calls += 1
        raise RuntimeError("private backend payload")

    outcome = run_bounded(broken, timeout_seconds=0.2)
    assert outcome.status == "degraded"
    assert outcome.error_code == "backend_error"
    assert outcome.value is None
    assert "private" not in repr(outcome)
    assert calls == 1


def test_caps_are_deterministic_and_bound_records_without_mutating_inputs():
    assert cap_text("alpha beta gamma", max_chars=10) == "alpha beta"
    assert cap_string_list(["a" * 20, "b", "c"], max_items=2, max_chars=8) == ["a" * 8]

    records = [
        {"id": "1", "content": "x" * 20, "private": "drop"},
        {"id": "2", "content": "short", "private": "drop"},
        {"id": "3", "content": "ignored"},
    ]
    capped = cap_records(
        records,
        max_items=2,
        max_chars=16,
        allowed_keys=("id", "content"),
    )
    assert capped == [{"id": "1", "content": "x" * 15}]
    assert records[0]["content"] == "x" * 20
