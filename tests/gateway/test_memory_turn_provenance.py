from __future__ import annotations

import json

from agent.memory_provenance import (
    consume_memory_ingress,
    mint_turn_provenance,
)
from gateway.config import Platform
from gateway.run import _issue_post_auth_memory_ingress
from gateway.session import SessionSource
from gateway.session_context import clear_session_vars, set_session_vars


def _source(
    *,
    user_id: str | None = "owner-1",
    chat_type: str = "dm",
    platform: Platform = Platform.TELEGRAM,
    is_bot: bool = False,
):
    return SessionSource(
        platform=platform,
        chat_id="chat-1",
        chat_type=chat_type,
        user_id=user_id,
        message_id="message-1",
        profile="default",
        is_bot=is_bot,
    )


def _bindings_file(tmp_path):
    path = tmp_path / "memory-subjects.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "hermes-memory-subject-bindings/v1",
                "bindings": [
                    {
                        "platform": "telegram",
                        "principal_id": "owner-1",
                        "subject_id": "danny",
                    },
                    {
                        "platform": "discord",
                        "principal_id": "owner-1",
                        "subject_id": "danny",
                    },
                    {
                        "platform": "local",
                        "principal_id": "owner-1",
                        "subject_id": "danny",
                    },
                ],
            }
        )
    )
    path.chmod(0o600)
    return path


def _turn(ingress):
    return mint_turn_provenance(
        ingress,
        session_id="session-1",
        turn_id="turn-1",
        message_id="message-1",
    )


def test_telegram_private_and_group_owner_bind_danny_after_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_MEMORY_SUBJECT_BINDINGS_FILE", str(_bindings_file(tmp_path)))
    private = _turn(
        _issue_post_auth_memory_ingress(_source(chat_type="dm"), internal=False)
    )
    group = _turn(
        _issue_post_auth_memory_ingress(_source(chat_type="group"), internal=False)
    )
    assert (private.origin, private.subject_id, private.authorizes_private_memory) == (
        "telegram_private",
        "danny",
        True,
    )
    assert (group.origin, group.subject_id, group.authorizes_private_memory) == (
        "telegram_group",
        "danny",
        True,
    )


def test_other_participant_internal_restored_bot_and_anonymous_are_denied(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_MEMORY_SUBJECT_BINDINGS_FILE", str(_bindings_file(tmp_path)))
    cases = [
        _issue_post_auth_memory_ingress(_source(user_id="other"), internal=False),
        _issue_post_auth_memory_ingress(_source(), internal=True),
        _issue_post_auth_memory_ingress(_source(), internal=False, restored=True),
        _issue_post_auth_memory_ingress(_source(is_bot=True), internal=False),
        _issue_post_auth_memory_ingress(_source(user_id=None), internal=False),
    ]
    assert all(not _turn(case).authorizes_private_memory for case in cases)


def test_api_transport_auth_is_not_a_private_subject(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_MEMORY_SUBJECT_BINDINGS_FILE", str(_bindings_file(tmp_path)))
    api = _turn(
        _issue_post_auth_memory_ingress(
            _source(platform=Platform.API_SERVER), internal=False
        )
    )
    assert api.origin == "photon_api"
    assert api.subject_id == "unidentified"
    assert not api.authorizes_private_memory


def test_delegated_and_noninteractive_local_gateway_origins_are_always_denied(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_MEMORY_SUBJECT_BINDINGS_FILE", str(_bindings_file(tmp_path)))
    discord = _turn(
        _issue_post_auth_memory_ingress(
            _source(platform=Platform.DISCORD), internal=False
        )
    )
    local_gateway = _turn(
        _issue_post_auth_memory_ingress(
            _source(platform=Platform.LOCAL), internal=False
        )
    )
    assert discord.origin == "delegated"
    assert local_gateway.origin == "tui"
    assert not discord.authorizes_private_memory
    assert not local_gateway.authorizes_private_memory


def test_session_context_explicitly_overwrites_inherited_ingress_with_deny(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_MEMORY_SUBJECT_BINDINGS_FILE", str(_bindings_file(tmp_path)))
    trusted = _issue_post_auth_memory_ingress(_source(), internal=False)
    first = set_session_vars(memory_ingress=trusted)
    try:
        second = set_session_vars(source="cron")
        try:
            denied = consume_memory_ingress()
            assert denied is not None
            assert not _turn(denied).authorizes_private_memory
            assert consume_memory_ingress() is None
        finally:
            clear_session_vars(second)
    finally:
        clear_session_vars(first)


def test_session_source_never_serializes_provenance(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_MEMORY_SUBJECT_BINDINGS_FILE", str(_bindings_file(tmp_path)))
    source = _source()
    _issue_post_auth_memory_ingress(source, internal=False)
    payload = source.to_dict()
    assert "memory" not in payload
    assert "provenance" not in payload
