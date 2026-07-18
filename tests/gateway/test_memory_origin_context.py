from agent.memory_provenance import (
    current_memory_origin,
    issue_authenticated_origin,
    issue_turn_envelope,
    principal_matches,
    validate_turn_envelope,
)
from gateway.run import _issue_post_auth_memory_origin
from gateway.session import Platform, SessionSource
from gateway.session_context import clear_session_vars, reset_session_vars, set_session_vars


def _source(*, user_id="owner-1", is_bot=False):
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="group",
        user_id=user_id,
        is_bot=is_bot,
        profile="default",
        message_id="message-1",
    )


def _is_valid(receipt, *, principal_id="owner-1"):
    envelope = issue_turn_envelope(
        receipt,
        session_id="session-1",
        turn_id="turn-1",
        message_id="message-1",
        user_content="hello",
    )
    return (
        validate_turn_envelope(
            envelope,
            user_content="hello",
            session_id="session-1",
            turn_id="turn-1",
            consume=False,
        ),
        principal_matches(envelope, platform="telegram", principal_id=principal_id),
    )


def test_post_auth_gateway_factory_mints_human_origin_without_serializing_it():
    source = _source()

    receipt = _issue_post_auth_memory_origin(source, internal=False)

    assert _is_valid(receipt) == (True, True)
    assert "memory" not in source.to_dict()
    assert "origin" not in source.to_dict()


def test_internal_bot_and_anonymous_gateway_events_are_explicit_deny_origins():
    assert _is_valid(_issue_post_auth_memory_origin(_source(), internal=True))[0] is False
    assert _is_valid(_issue_post_auth_memory_origin(_source(is_bot=True), internal=False))[0] is False
    assert _is_valid(_issue_post_auth_memory_origin(_source(user_id=None), internal=False))[0] is False


def test_session_context_binds_and_clears_origin_receipt():
    receipt = issue_authenticated_origin(
        runtime_class="gateway",
        origin_class="authenticated_human_gateway",
        platform="telegram",
        profile="default",
        principal_id="owner-1",
        adapter_receipt_id="auth-1",
        source_observation_id="observation-1",
    )
    tokens = set_session_vars(
        source="telegram",
        session_id="session-1",
        memory_origin_receipt=receipt,
    )
    try:
        assert current_memory_origin() is receipt
    finally:
        clear_session_vars(tokens)

    assert current_memory_origin() is None


def test_session_context_without_receipt_overwrites_inherited_origin_with_deny():
    receipt = issue_authenticated_origin(
        runtime_class="gateway",
        origin_class="authenticated_human_gateway",
        platform="telegram",
        profile="default",
        principal_id="owner-1",
        adapter_receipt_id="auth-1",
        source_observation_id="observation-1",
    )
    first = set_session_vars(memory_origin_receipt=receipt)
    try:
        second = set_session_vars(source="tui")
        try:
            assert _is_valid(current_memory_origin())[0] is False
        finally:
            clear_session_vars(second)
    finally:
        clear_session_vars(first)
        reset_session_vars()

    assert current_memory_origin() is None
