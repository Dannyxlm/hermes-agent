from dataclasses import fields

import pytest

from agent.memory_provenance import (
    MemoryToolReceipt,
    MemoryTurnEnvelope,
    MemoryWriteReceipt,
    authenticated_local_cli_origin,
    bind_memory_origin,
    clear_memory_origin,
    issue_authenticated_origin,
    issue_deny_origin,
    issue_memory_tool_receipt,
    issue_memory_write_receipt,
    issue_turn_envelope,
    principal_matches,
    validate_memory_tool_receipt,
    validate_turn_envelope,
    validate_memory_write_receipt,
)


def _origin():
    return issue_authenticated_origin(
        runtime_class="gateway",
        origin_class="authenticated_human_gateway",
        platform="telegram",
        profile="default",
        principal_id="fixture-user-123",
        principal_alias="owner",
        adapter_receipt_id="adapter-receipt-1",
        source_observation_id="source-observation-1",
        policy_revision="u3-fixture-v1",
        writer_release="fixture-release",
    )


def _turn(origin=None, *, issued_at=None):
    return issue_turn_envelope(
        origin or _origin(),
        session_id="session-1",
        turn_id="turn-1",
        message_id="message-1",
        user_content="I prefer concise answers",
        issued_at=issued_at,
    )


def test_authenticated_origin_mints_valid_content_free_turn_envelope():
    envelope = _turn()

    assert isinstance(envelope, MemoryTurnEnvelope)
    assert validate_turn_envelope(
        envelope,
        user_content="I prefer concise answers",
        session_id="session-1",
        turn_id="turn-1",
        consume=False,
    ) is True
    assert principal_matches(envelope, platform="telegram", principal_id="fixture-user-123")
    assert not principal_matches(envelope, platform="telegram", principal_id="other-user")
    assert "I prefer concise answers" not in repr(envelope)
    assert "user_content" not in {field.name for field in fields(envelope)}


def test_plain_dict_and_deny_origin_never_validate():
    assert validate_turn_envelope(
        {"origin_class": "authenticated_human_gateway"},
        user_content="hello",
        session_id="session-1",
        turn_id="turn-1",
        consume=False,
    ) is False

    denied = issue_deny_origin(
        runtime_class="gateway",
        origin_class="synthetic_internal",
        platform="telegram",
        profile="default",
        policy_revision="u3-fixture-v1",
        writer_release="fixture-release",
    )
    envelope = issue_turn_envelope(
        denied,
        session_id="session-1",
        turn_id="turn-1",
        message_id="message-1",
        user_content="hello",
    )
    assert validate_turn_envelope(
        envelope,
        user_content="hello",
        session_id="session-1",
        turn_id="turn-1",
        consume=False,
    ) is False


def test_envelope_is_bound_to_content_session_and_turn():
    envelope = _turn()

    assert not validate_turn_envelope(
        envelope,
        user_content="changed",
        session_id="session-1",
        turn_id="turn-1",
        consume=False,
    )
    assert not validate_turn_envelope(
        envelope,
        user_content="I prefer concise answers",
        session_id="other-session",
        turn_id="turn-1",
        consume=False,
    )
    assert not validate_turn_envelope(
        envelope,
        user_content="I prefer concise answers",
        session_id="session-1",
        turn_id="other-turn",
        consume=False,
    )


def test_valid_envelope_is_single_use_when_consumed():
    envelope = _turn()

    assert validate_turn_envelope(
        envelope,
        user_content="I prefer concise answers",
        session_id="session-1",
        turn_id="turn-1",
        consume=True,
    )
    assert not validate_turn_envelope(
        envelope,
        user_content="I prefer concise answers",
        session_id="session-1",
        turn_id="turn-1",
        consume=True,
    )


def test_stale_envelope_fails_closed():
    envelope = _turn(issued_at=1.0)

    assert not validate_turn_envelope(
        envelope,
        user_content="I prefer concise answers",
        session_id="session-1",
        turn_id="turn-1",
        consume=False,
        now=100.0,
        max_age_seconds=10.0,
    )


def test_origin_context_binding_is_task_local_and_clearable():
    receipt = _origin()
    token = bind_memory_origin(receipt)
    try:
        envelope = issue_turn_envelope(
            None,
            session_id="session-2",
            turn_id="turn-2",
            message_id="message-2",
            user_content="hello",
        )
        assert validate_turn_envelope(
            envelope,
            user_content="hello",
            session_id="session-2",
            turn_id="turn-2",
            consume=False,
        )
    finally:
        clear_memory_origin(token)

    denied = issue_turn_envelope(
        None,
        session_id="session-3",
        turn_id="turn-3",
        message_id="message-3",
        user_content="hello",
    )
    assert not validate_turn_envelope(
        denied,
        user_content="hello",
        session_id="session-3",
        turn_id="turn-3",
        consume=False,
    )


def test_local_cli_context_is_explicit_and_cleared_after_one_invocation():
    with authenticated_local_cli_origin(profile="default"):
        envelope = issue_turn_envelope(
            None,
            session_id="cli-session",
            turn_id="cli-turn",
            message_id="cli-message",
            user_content="hello",
        )
        assert envelope.origin_class == "authenticated_human_cli"
        assert envelope.principal_alias == "local_owner"
        assert validate_turn_envelope(
            envelope,
            user_content="hello",
            session_id="cli-session",
            turn_id="cli-turn",
            consume=False,
        )

    denied = issue_turn_envelope(
        None,
        session_id="later",
        turn_id="later",
        message_id="later",
        user_content="hello",
    )
    assert denied.human_authored is False


def test_committed_memory_write_receipt_is_content_free_bound_and_single_use():
    envelope = _turn()
    receipt = issue_memory_write_receipt(
        envelope,
        tool_call_id="tool-call-1",
        action="add",
        target="user",
        content="Danny prefers concise answers",
        committed=True,
    )

    assert isinstance(receipt, MemoryWriteReceipt)
    assert "Danny prefers concise answers" not in repr(receipt)
    assert "content" not in {field.name for field in fields(receipt)}
    assert principal_matches(receipt, platform="telegram", principal_id="fixture-user-123")
    assert validate_memory_write_receipt(
        receipt,
        turn_envelope=None,
        tool_call_id="tool-call-1",
        action="add",
        target="user",
        content="Danny prefers concise answers",
        consume=False,
    )
    assert validate_memory_write_receipt(
        receipt,
        turn_envelope=envelope,
        tool_call_id="tool-call-1",
        action="add",
        target="user",
        content="Danny prefers concise answers",
        consume=True,
    )
    assert not validate_memory_write_receipt(
        receipt,
        turn_envelope=envelope,
        tool_call_id="tool-call-1",
        action="add",
        target="user",
        content="Danny prefers concise answers",
        consume=True,
    )


def test_failed_or_untrusted_memory_write_cannot_mint_receipt():
    assert issue_memory_write_receipt(
        _turn(),
        tool_call_id="tool-call-1",
        action="add",
        target="user",
        content="fact",
        committed=False,
    ) is None

    denied_origin = issue_deny_origin(origin_class="background_review")
    denied_envelope = issue_turn_envelope(
        denied_origin,
        session_id="s",
        turn_id="t",
        message_id="m",
        user_content="prompt",
    )
    assert issue_memory_write_receipt(
        denied_envelope,
        tool_call_id="tool-call-1",
        action="add",
        target="user",
        content="fact",
        committed=True,
    ) is None


def test_tool_receipt_is_sealed_content_free_principal_bound_and_single_use():
    turn = _turn()
    receipt = issue_memory_tool_receipt(
        turn,
        tool_name="honcho_conclude",
        tool_call_id="call-123",
    )
    assert isinstance(receipt, MemoryToolReceipt)
    assert "I prefer concise answers" not in repr(receipt)
    assert "content" not in {field.name for field in fields(receipt)}
    assert len(receipt.session_hmac) == 64
    assert len(receipt.tool_call_hmac) == 64
    assert receipt.session_id not in receipt.session_hmac
    assert receipt.tool_call_id not in receipt.tool_call_hmac
    assert principal_matches(
        receipt,
        principal_id="fixture-user-123",
        platform="telegram",
    )
    assert not validate_memory_tool_receipt(
        receipt,
        tool_name="honcho_search",
        tool_call_id="call-123",
        consume=False,
    )
    assert validate_memory_tool_receipt(
        receipt,
        tool_name="honcho_conclude",
        tool_call_id="call-123",
        consume=True,
    )
    assert not validate_memory_tool_receipt(
        receipt,
        tool_name="honcho_conclude",
        tool_call_id="call-123",
        consume=True,
    )
