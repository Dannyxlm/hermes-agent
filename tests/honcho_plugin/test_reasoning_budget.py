"""Behavioral tests for the isolated Honcho paid-reasoning guard."""

from __future__ import annotations

import threading
import time
import json
import os

import pytest

from agent.memory_provenance import (
    issue_authenticated_origin,
    issue_memory_tool_receipt,
    issue_turn_envelope,
)

from plugins.memory.honcho.reasoning_budget import (
    FAILURE,
    MAX,
    MEDIUM,
    MINIMAL,
    LOW,
    OFFLINE,
    INTERACTIVE,
    SUCCESS,
    TIMEOUT,
    UNKNOWN_OUTCOME,
    InMemoryReceiptSink,
    InMemoryReservationStore,
    OfflineReasoningManifest,
    PaidReasoningGuard,
    UnknownOutcomeError,
    append_public_reasoning_receipt,
    build_public_reasoning_receipt,
    issue_explicit_approval,
    issue_trusted_tool_call_id,
    is_trusted_tool_call_id,
    validate_public_reasoning_receipt,
)


SECRET = "test-paid-reasoning-secret"


def _guard(*, sink=None, reservations=None, manifest=None):
    sink = sink or InMemoryReceiptSink()
    reservations = reservations or InMemoryReservationStore()
    return PaidReasoningGuard(
        hmac_secret=SECRET,
        receipt_sink=sink,
        reservation_store=reservations,
        offline_manifest=manifest,
    ), sink, reservations


def _call_id():
    return issue_trusted_tool_call_id(SECRET)


def _executor_receipt():
    origin = issue_authenticated_origin(
        runtime_class="gateway",
        origin_class="authenticated_human_gateway",
        platform="telegram",
        profile="default",
        principal_id="fixture-danny",
        adapter_receipt_id="adapter",
        source_observation_id="observation",
    )
    turn = issue_turn_envelope(
        origin,
        session_id="private-session",
        turn_id="private-turn",
        message_id="private-message",
        user_content="private query content",
    )
    return issue_memory_tool_receipt(
        turn,
        tool_name="honcho_reasoning",
        tool_call_id="runtime-tool-call",
    )


def test_tool_call_ids_are_trusted_opaque_hmac_tokens():
    call_id = _call_id()

    assert is_trusted_tool_call_id(SECRET, call_id)
    assert not is_trusted_tool_call_id(SECRET, call_id + "x")
    assert "what is the user's name" not in call_id
    assert "test-paid-reasoning-secret" not in call_id


def test_interactive_allows_only_minimal_low_and_medium():
    guard, sink, _ = _guard()
    calls = []

    for tier in (MINIMAL, LOW, MEDIUM):
        result = guard.execute(
            lambda tier=tier: calls.append(tier) or f"answer-{tier}",
            tier=tier,
            tool_call_id=_call_id(),
            mode=INTERACTIVE,
        )
        assert result.allowed is True
        assert result.status == SUCCESS

    assert calls == [MINIMAL, LOW, MEDIUM]
    assert [receipt.status for receipt in sink.receipts()] == [SUCCESS] * 3


def test_interactive_high_and_max_are_denied_without_invoking_callable():
    guard, _, _ = _guard()
    calls = []

    for tier in ("high", MAX):
        result = guard.execute(
            lambda: calls.append("called"),
            tier=tier,
            tool_call_id=_call_id(),
            mode=INTERACTIVE,
        )
        assert result.allowed is False
        assert result.status == "denied"

    assert calls == []


def test_offline_high_requires_exact_manifest_call_id_and_tier():
    call_id = _call_id()
    manifest = OfflineReasoningManifest([(call_id, "high")])
    guard, _, _ = _guard(manifest=manifest)
    calls = []

    accepted = guard.execute(
        lambda: calls.append("accepted") or "answer",
        tier="high",
        tool_call_id=call_id,
        mode=OFFLINE,
    )
    denied_tier = guard.execute(
        lambda: calls.append("wrong-tier"),
        tier=MAX,
        tool_call_id=call_id,
        mode=OFFLINE,
    )
    denied_id = guard.execute(
        lambda: calls.append("wrong-id"),
        tier="high",
        tool_call_id=_call_id(),
        mode=OFFLINE,
    )

    assert accepted.status == SUCCESS
    assert denied_tier.status == "denied"
    assert denied_id.status == "denied"
    assert calls == ["accepted"]


def test_offline_max_requires_manifest_and_exact_explicit_approval():
    call_id = _call_id()
    manifest = OfflineReasoningManifest({call_id: MAX})
    approval = issue_explicit_approval(SECRET, call_id=call_id, tier=MAX)
    guard, _, _ = _guard(manifest=manifest)
    calls = []

    denied = guard.execute(
        lambda: calls.append("wrong-approval"),
        tier=MAX,
        tool_call_id=call_id,
        mode=OFFLINE,
        explicit_approval=approval + "x",
    )
    accepted = guard.execute(
        lambda: calls.append("accepted") or "answer",
        tier=MAX,
        tool_call_id=call_id,
        mode=OFFLINE,
        explicit_approval=approval,
    )

    assert denied.status == "denied"
    assert accepted.status == SUCCESS
    assert calls == ["accepted"]


def test_invalid_tier_is_denied_before_reservation_and_call():
    guard, _, reservations = _guard()
    calls = []

    result = guard.execute(
        lambda: calls.append("called"),
        tier="turbo",
        tool_call_id=_call_id(),
        mode=INTERACTIVE,
    )

    assert result.status == "denied"
    assert "invalid" in result.reason
    assert calls == []
    assert reservations.reservations() == []


def test_duplicate_call_id_is_denied_with_zero_second_calls():
    guard, sink, _ = _guard()
    call_id = _call_id()
    calls = []

    first = guard.execute(
        lambda: calls.append("first") or "answer",
        tier=LOW,
        tool_call_id=call_id,
    )
    second = guard.execute(
        lambda: calls.append("second"),
        tier=LOW,
        tool_call_id=call_id,
    )

    assert first.status == SUCCESS
    assert second.status == "denied"
    assert "duplicate" in second.reason
    assert calls == ["first"]
    assert len(sink.receipts()) == 1


def test_concurrent_reservation_allows_exactly_one_callable():
    guard, _, _ = _guard()
    call_id = _call_id()
    barrier = threading.Barrier(2)
    calls = []
    results = []

    def attempt():
        barrier.wait()
        results.append(
            guard.execute(
                lambda: calls.append("called") or "answer",
                tier=LOW,
                tool_call_id=call_id,
            )
        )

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [result.status for result in results].count(SUCCESS) == 1
    assert [result.status for result in results].count("denied") == 1
    assert calls == ["called"]


def test_reservation_failure_denies_without_call():
    class RejectingReservations:
        def reserve(self, call_id, tier):
            return False

    guard, sink, _ = _guard(reservations=RejectingReservations())
    calls = []

    result = guard.execute(
        lambda: calls.append("called"),
        tier=LOW,
        tool_call_id=_call_id(),
    )

    assert result.status == "denied"
    assert "reservation" in result.reason
    assert calls == []
    assert sink.receipts() == []


@pytest.mark.parametrize(
    ("callable_factory", "expected"),
    [
        (lambda: (lambda: "answer"), SUCCESS),
        (lambda: (lambda: (_ for _ in ()).throw(RuntimeError("provider failed"))), FAILURE),
        (lambda: (lambda: (_ for _ in ()).throw(TimeoutError())), TIMEOUT),
        (lambda: (lambda: (_ for _ in ()).throw(UnknownOutcomeError())), UNKNOWN_OUTCOME),
    ],
)
def test_accepted_call_has_one_invocation_and_one_terminal_receipt(callable_factory, expected):
    guard, sink, _ = _guard()
    invocations = []
    supplied = callable_factory()

    def counted_call():
        invocations.append("called")
        return supplied()

    result = guard.execute(
        counted_call,
        tier=LOW,
        tool_call_id=_call_id(),
    )

    assert result.status == expected
    assert invocations == ["called"]
    assert [receipt.status for receipt in sink.receipts()] == [expected]


def test_deadline_returns_without_waiting_for_non_daemon_worker_and_late_result_is_isolated():
    guard, sink, _ = _guard()
    started = threading.Event()
    release = threading.Event()
    late_call_id = _call_id()

    def slow_call():
        started.set()
        release.wait(2)
        return "late answer"

    began = time.monotonic()
    timed_out = guard.execute(
        slow_call,
        tier=LOW,
        tool_call_id=late_call_id,
        deadline=time.monotonic() + 0.05,
    )
    elapsed = time.monotonic() - began

    assert started.wait(0.5)
    assert timed_out.status == TIMEOUT
    assert elapsed < 0.5
    assert timed_out.worker_thread is not None
    assert timed_out.worker_thread.daemon is True

    other = guard.execute(
        lambda: "other answer",
        tier=LOW,
        tool_call_id=_call_id(),
    )
    assert other.status == SUCCESS

    release.set()
    timed_out.worker_thread.join(1)
    assert [receipt.status for receipt in sink.receipts()] == [TIMEOUT, SUCCESS]
    assert sink.get(late_call_id).status == TIMEOUT


def test_unknown_outcome_marker_is_terminal_without_retry():
    guard, sink, _ = _guard()
    invocations = []

    def provider_call():
        invocations.append("called")
        return UNKNOWN_OUTCOME

    result = guard.execute(
        provider_call,
        tier=LOW,
        tool_call_id=_call_id(),
    )

    assert result.status == UNKNOWN_OUTCOME
    assert invocations == ["called"]
    assert sink.receipts()[0].status == UNKNOWN_OUTCOME


def test_guard_result_repr_never_exposes_answer_or_exception_payload():
    guard, _, _ = _guard()
    success = guard.execute(
        lambda: "private synthesized answer",
        tier=LOW,
        tool_call_id=_call_id(),
    )
    failure = guard.execute(
        lambda: (_ for _ in ()).throw(RuntimeError("private backend payload")),
        tier=LOW,
        tool_call_id=_call_id(),
    )
    assert "private synthesized answer" not in repr(success)
    assert "private backend payload" not in repr(failure)


def test_public_reasoning_receipts_are_content_free_schema_valid_and_fsynced(tmp_path):
    executor = _executor_receipt()
    call_id = _call_id()
    reserved = build_public_reasoning_receipt(
        tool_receipt=executor,
        reasoning_call_id=call_id,
        phase="reserved",
        status="reserved",
        tier=LOW,
        estimated_cost_usd=0.02,
        observed_at="2026-07-18T21:00:00Z",
    )
    terminal = build_public_reasoning_receipt(
        tool_receipt=executor,
        reasoning_call_id=call_id,
        phase="terminal",
        status="succeeded",
        tier=LOW,
        estimated_cost_usd=0.02,
        observed_at="2026-07-18T21:00:01Z",
    )
    assert validate_public_reasoning_receipt(reserved)
    assert validate_public_reasoning_receipt(terminal)
    serialized = json.dumps([reserved, terminal])
    for private_value in (
        "private-session", "private-turn", "runtime-tool-call", "private query content"
    ):
        assert private_value not in serialized

    ledger = tmp_path / "reasoning.jsonl"
    assert append_public_reasoning_receipt(str(ledger), reserved)
    assert append_public_reasoning_receipt(str(ledger), terminal)
    assert os.stat(ledger).st_mode & 0o077 == 0
    lines = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [line["phase"] for line in lines] == ["reserved", "terminal"]
    assert lines[0]["reasoning_call_id"] == lines[1]["reasoning_call_id"]


def test_reasoning_receipt_append_rejects_symlink(tmp_path):
    receipt = build_public_reasoning_receipt(
        tool_receipt=_executor_receipt(),
        reasoning_call_id=_call_id(),
        phase="reserved",
        status="reserved",
        tier=LOW,
        estimated_cost_usd=0.01,
    )
    target = tmp_path / "target.jsonl"
    target.write_text("")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    assert append_public_reasoning_receipt(str(link), receipt) is False
