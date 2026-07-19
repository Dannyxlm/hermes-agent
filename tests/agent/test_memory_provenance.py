from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import agent.memory_provenance as provenance_module

from agent.memory_provenance import (
    MemoryProvenanceError,
    build_memory_proxy_proof,
    consume_memory_ingress,
    issue_authenticated_ingress,
    issue_synthetic_ingress,
    load_memory_subject_bindings,
    local_interactive_memory_ingress,
    memory_ingress_scope,
    mint_turn_provenance,
    validate_memory_turn_provenance,
    verify_memory_proxy_proof,
)


def _private_file(path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def _proof_env(monkeypatch, tmp_path) -> tuple[str, str]:
    key_path = tmp_path / "memory-proof.key"
    state_path = tmp_path / "memory-proof-replay.json"
    _private_file(key_path, os.urandom(48))
    monkeypatch.setenv("HERMES_MEMORY_PROXY_PROOF_KEY_FILE", str(key_path))
    monkeypatch.setenv("HERMES_MEMORY_PROXY_PROOF_KEY_ID", "proof-key-2026-07")
    monkeypatch.setenv("HERMES_MEMORY_PROXY_PROOF_AUDIENCE", "ava-memory-api")
    monkeypatch.setenv("HERMES_MEMORY_PROXY_REPLAY_STATE", str(state_path))
    return str(key_path), str(state_path)


def test_provenance_serializes_exact_canonical_v1_without_runtime_bindings():
    ingress = issue_authenticated_ingress(
        origin="telegram_private",
        platform="telegram",
        principal_id="raw-telegram-user-123",
        subject_id="danny",
    )
    provenance = mint_turn_provenance(
        ingress,
        session_id="raw-session-1",
        turn_id="raw-turn-1",
        message_id="raw-message-1",
    )

    assert provenance.to_protocol_dict() == {
        "schema_version": "memory-provenance/v1",
        "provenance_id": provenance.provenance_id,
        "caller_id": provenance.caller_id,
        "subject_id": "danny",
        "origin": "telegram_private",
        "authenticated_at": provenance.authenticated_at,
        "deployment_mode": "tools_only_read_containment",
    }
    rendered = json.dumps(provenance.to_protocol_dict())
    for raw in ("raw-telegram-user-123", "raw-session-1", "raw-turn-1", "raw-message-1"):
        assert raw not in rendered
        assert raw not in repr(provenance)
    assert validate_memory_turn_provenance(
        provenance,
        session_id="raw-session-1",
        turn_id="raw-turn-1",
        message_id="raw-message-1",
    )
    assert not validate_memory_turn_provenance(
        provenance,
        session_id="raw-session-1",
        turn_id="other-turn",
        message_id="raw-message-1",
    )


def test_provenance_is_frozen_and_caller_is_distinct_from_subject():
    ingress = issue_authenticated_ingress(
        origin="telegram_group",
        platform="telegram",
        principal_id="owner-1",
        subject_id="danny",
    )
    provenance = mint_turn_provenance(
        ingress, session_id="s", turn_id="t", message_id="m"
    )
    assert provenance.caller_id != provenance.subject_id
    with pytest.raises(dataclasses.FrozenInstanceError):
        provenance.subject_id = "attacker"


def test_synthetic_and_unidentified_ingress_are_local_only():
    synthetic = mint_turn_provenance(
        issue_synthetic_ingress(origin="background", reason="review"),
        session_id="s",
        turn_id="t",
        message_id="m",
    )
    unidentified = mint_turn_provenance(
        issue_authenticated_ingress(
            origin="telegram_group",
            platform="telegram",
            principal_id="other-user",
            subject_id="unidentified",
        ),
        session_id="s2",
        turn_id="t2",
        message_id="m2",
    )
    assert synthetic.deployment_mode == "local_only"
    assert not synthetic.authorizes_private_memory
    assert unidentified.deployment_mode == "local_only"
    assert not unidentified.authorizes_private_memory


def test_ingress_context_is_one_shot_and_never_ambient_authority():
    ingress = issue_authenticated_ingress(
        origin="cli",
        platform="local",
        principal_id="uid-501",
        subject_id="local-owner",
    )
    with memory_ingress_scope(ingress):
        assert consume_memory_ingress() is ingress
        assert consume_memory_ingress() is None
    assert consume_memory_ingress() is None


class _TTY:
    def __init__(self, value: bool):
        self.value = value

    def isatty(self):
        return self.value


def test_local_owner_requires_real_interactive_boundary(monkeypatch, tmp_path):
    local_uid = str(getattr(os, "geteuid", lambda: "local")())
    bindings = tmp_path / "subjects.json"
    _private_file(
        bindings,
        json.dumps(
            {
                "schema_version": "hermes-memory-subject-bindings/v1",
                "bindings": [
                    {
                        "platform": "local",
                        "principal_id": f"uid:{local_uid}",
                        "subject_id": "danny",
                    }
                ],
            },
            separators=(",", ":"),
        ).encode(),
    )
    monkeypatch.setenv("HERMES_MEMORY_SUBJECT_BINDINGS_FILE", str(bindings))
    interactive = local_interactive_memory_ingress(
        origin="cli", stdin=_TTY(True), stdout=_TTY(True)
    )
    piped = local_interactive_memory_ingress(
        origin="cli", stdin=_TTY(False), stdout=_TTY(True)
    )
    assert interactive.authenticated is True
    assert interactive.subject_id == "danny"
    assert piped.authenticated is False
    assert piped.subject_id == "unidentified"


def test_private_subject_binding_loader_and_digest(tmp_path):
    path = tmp_path / "subjects.json"
    _private_file(
        path,
        json.dumps(
            {
                "schema_version": "hermes-memory-subject-bindings/v1",
                "bindings": [
                    {
                        "platform": "telegram",
                        "principal_id": "raw-owner-id",
                        "subject_id": "danny",
                    }
                ],
            },
            separators=(",", ":"),
        ).encode(),
    )

    loaded = load_memory_subject_bindings(path)
    assert loaded.resolve("telegram", "raw-owner-id") == "danny"
    assert loaded.resolve("telegram", "someone-else") == "unidentified"
    assert len(loaded.content_digest) == 64
    assert "raw-owner-id" not in repr(loaded)


def test_subject_binding_loader_rejects_duplicates_symlinks_and_public_mode(tmp_path):
    duplicate = tmp_path / "duplicate.json"
    _private_file(
        duplicate,
        json.dumps(
            {
                "schema_version": "hermes-memory-subject-bindings/v1",
                "bindings": [
                    {"platform": "telegram", "principal_id": "1", "subject_id": "danny"},
                    {"platform": "telegram", "principal_id": "1", "subject_id": "other"},
                ],
            }
        ).encode(),
    )
    with pytest.raises(MemoryProvenanceError) as exc:
        load_memory_subject_bindings(duplicate)
    assert exc.value.code == "duplicate_subject_binding"

    public = tmp_path / "public.json"
    _private_file(public, b'{"schema_version":"hermes-memory-subject-bindings/v1","bindings":[]}')
    public.chmod(0o644)
    with pytest.raises(MemoryProvenanceError) as exc:
        load_memory_subject_bindings(public)
    assert exc.value.code == "unsafe_subject_bindings_file"

    link = tmp_path / "subjects-link.json"
    link.symlink_to(duplicate)
    with pytest.raises(MemoryProvenanceError):
        load_memory_subject_bindings(link)


def test_subject_binding_fifo_is_rejected_without_blocking(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO files are unavailable on this platform")
    fifo = tmp_path / "subjects.fifo"
    os.mkfifo(fifo, 0o600)

    with pytest.raises(MemoryProvenanceError) as exc:
        load_memory_subject_bindings(fifo)

    assert exc.value.code == "unsafe_subject_bindings_file"


def test_proxy_key_fifo_and_traversing_replay_path_are_rejected(
    monkeypatch, tmp_path
):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO files are unavailable on this platform")
    key_path = tmp_path / "proof-key.fifo"
    os.mkfifo(key_path, 0o600)
    monkeypatch.setenv("HERMES_MEMORY_PROXY_PROOF_KEY_FILE", str(key_path))
    monkeypatch.setenv("HERMES_MEMORY_PROXY_PROOF_KEY_ID", "proof-key-v1")
    monkeypatch.setenv("HERMES_MEMORY_PROXY_PROOF_AUDIENCE", "test-api")
    monkeypatch.setenv(
        "HERMES_MEMORY_PROXY_REPLAY_STATE", str(tmp_path / "replay.json")
    )
    ingress = issue_authenticated_ingress(
        origin="telegram_private",
        platform="telegram",
        principal_id="owner",
        subject_id="danny",
    )

    with pytest.raises(MemoryProvenanceError) as exc:
        build_memory_proxy_proof(
            b"{}", method="POST", path="/v1/chat/completions", ingress=ingress
        )
    assert exc.value.code == "unsafe_proxy_proof_key"

    key_path.unlink()
    _private_file(key_path, os.urandom(48))
    monkeypatch.setenv(
        "HERMES_MEMORY_PROXY_REPLAY_STATE",
        str(tmp_path / "nested" / ".." / "replay.json"),
    )
    with pytest.raises(MemoryProvenanceError) as exc:
        build_memory_proxy_proof(
            b"{}", method="POST", path="/v1/chat/completions", ingress=ingress
        )
    assert exc.value.code == "unsafe_proxy_replay_state"


def test_proxy_proof_round_trip_binds_body_path_audience_and_identity(monkeypatch, tmp_path):
    _proof_env(monkeypatch, tmp_path)
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    body = b'{"messages":[{"role":"user","content":"hello"}],"stream":true}'
    ingress = issue_authenticated_ingress(
        origin="telegram_private",
        platform="telegram",
        principal_id="raw-owner-id",
        subject_id="danny",
        authenticated_at=now - timedelta(seconds=2),
    )

    proof = build_memory_proxy_proof(
        body,
        method="POST",
        path="/v1/chat/completions",
        ingress=ingress,
        now=now,
        nonce="fixed-test-nonce-0000000000000001",
    )
    verified = verify_memory_proxy_proof(
        proof,
        body,
        method="POST",
        path="/v1/chat/completions",
        now=now + timedelta(seconds=1),
    )
    assert verified.caller_id == ingress.caller_id
    assert verified.subject_id == "danny"
    assert verified.origin == "telegram_private"
    assert "raw-owner-id" not in proof


@pytest.mark.parametrize(
    ("body", "path", "seconds", "expected"),
    [
        (b'{"different":true}', "/v1/chat/completions", 1, "proxy_body_mismatch"),
        (b'{"ok":true}', "/v1/responses", 1, "proxy_path_mismatch"),
        (b'{"ok":true}', "/v1/chat/completions", 61, "proxy_proof_expired"),
    ],
)
def test_proxy_proof_tamper_and_expiry_fail_closed(
    monkeypatch, tmp_path, body, path, seconds, expected
):
    _proof_env(monkeypatch, tmp_path)
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    original = b'{"ok":true}'
    ingress = issue_authenticated_ingress(
        origin="telegram_private",
        platform="telegram",
        principal_id="owner",
        subject_id="danny",
        authenticated_at=now,
    )
    proof = build_memory_proxy_proof(
        original,
        method="POST",
        path="/v1/chat/completions",
        ingress=ingress,
        now=now,
    )

    with pytest.raises(MemoryProvenanceError) as exc:
        verify_memory_proxy_proof(
            proof,
            body,
            method="POST",
            path=path,
            now=now + timedelta(seconds=seconds),
        )
    assert exc.value.code == expected


def test_proxy_proof_replay_is_durable_and_key_rotation_does_not_reset_it(monkeypatch, tmp_path):
    key_path, _state_path = _proof_env(monkeypatch, tmp_path)
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    body = b'{"ok":true}'
    ingress = issue_authenticated_ingress(
        origin="telegram_group",
        platform="telegram",
        principal_id="owner",
        subject_id="danny",
        authenticated_at=now,
    )
    proof = build_memory_proxy_proof(
        body,
        method="POST",
        path="/v1/chat/completions",
        ingress=ingress,
        now=now,
        nonce="replay-test-nonce-0000000000000001",
    )
    verify_memory_proxy_proof(
        proof, body, method="POST", path="/v1/chat/completions", now=now
    )

    # Rotation changes signing material and key id, but the replay ledger is
    # independent and retains the consumed nonce from the old key generation.
    _private_file(__import__("pathlib").Path(key_path), os.urandom(48))
    monkeypatch.setenv("HERMES_MEMORY_PROXY_PROOF_KEY_ID", "proof-key-2026-08")
    with pytest.raises(MemoryProvenanceError) as exc:
        verify_memory_proxy_proof(
            proof, body, method="POST", path="/v1/chat/completions", now=now
        )
    assert exc.value.code in {"proxy_key_id_mismatch", "proxy_signature_invalid"}

    # Restore the old verifier material: the durable replay entry still denies.
    # (The original key bytes are deliberately unavailable in this test, so
    # mint a second verifier config with the same state and nonce.)
    second_key = tmp_path / "second-proof.key"
    _private_file(second_key, os.urandom(48))
    monkeypatch.setenv("HERMES_MEMORY_PROXY_PROOF_KEY_FILE", str(second_key))
    ingress2 = issue_authenticated_ingress(
        origin="telegram_group",
        platform="telegram",
        principal_id="owner",
        subject_id="danny",
        authenticated_at=now,
    )
    proof2 = build_memory_proxy_proof(
        body,
        method="POST",
        path="/v1/chat/completions",
        ingress=ingress2,
        now=now,
        nonce="replay-test-nonce-0000000000000001",
    )
    with pytest.raises(MemoryProvenanceError) as exc:
        verify_memory_proxy_proof(
            proof2, body, method="POST", path="/v1/chat/completions", now=now
        )
    assert exc.value.code == "proxy_proof_replayed"


def test_proxy_replay_lock_contention_fails_closed_without_waiting(monkeypatch, tmp_path):
    fcntl = pytest.importorskip("fcntl")
    _key_path, state_path = _proof_env(monkeypatch, tmp_path)
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    body = b'{"ok":true}'
    ingress = issue_authenticated_ingress(
        origin="telegram_private",
        platform="telegram",
        principal_id="owner",
        subject_id="danny",
        authenticated_at=now,
    )
    proof = build_memory_proxy_proof(
        body,
        method="POST",
        path="/v1/chat/completions",
        ingress=ingress,
        now=now,
    )
    lock_path = __import__("pathlib").Path(state_path + ".lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(MemoryProvenanceError) as exc:
            verify_memory_proxy_proof(
                proof,
                body,
                method="POST",
                path="/v1/chat/completions",
                now=now,
            )
        assert exc.value.code == "proxy_replay_lock_busy"
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def test_proxy_proof_key_must_be_distinct_from_transport_key(monkeypatch, tmp_path):
    key_path, _state_path = _proof_env(monkeypatch, tmp_path)
    shared_key = b"x" * 48
    _private_file(__import__("pathlib").Path(key_path), shared_key)
    monkeypatch.setenv("GATEWAY_PROXY_KEY", shared_key.decode("ascii"))
    ingress = issue_authenticated_ingress(
        origin="telegram_private",
        platform="telegram",
        principal_id="owner",
        subject_id="danny",
    )
    with pytest.raises(MemoryProvenanceError) as exc:
        build_memory_proxy_proof(
            b"{}", method="POST", path="/v1/chat/completions", ingress=ingress
        )
    assert exc.value.code == "proxy_key_reuse_denied"


def test_proxy_replay_capacity_never_evicts_live_nonces(monkeypatch, tmp_path):
    _key_path, state_path = _proof_env(monkeypatch, tmp_path)
    monkeypatch.setattr(provenance_module, "MAX_PROXY_REPLAY_ENTRIES", 2)
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    state = {
        "schema_version": "hermes-memory-proxy-replay/v1",
        "entries": [
            {"nonce_digest": hashlib.sha256(value.encode()).hexdigest(), "expires_at": int(now.timestamp()) + 30}
            for value in ("first-live-nonce", "second-live-nonce")
        ],
    }
    _private_file(
        __import__("pathlib").Path(state_path),
        json.dumps(state, separators=(",", ":")).encode(),
    )
    ingress = issue_authenticated_ingress(
        origin="telegram_private",
        platform="telegram",
        principal_id="owner",
        subject_id="danny",
        authenticated_at=now,
    )
    proof = build_memory_proxy_proof(
        b"{}",
        method="POST",
        path="/v1/chat/completions",
        ingress=ingress,
        now=now,
        nonce="third-live-nonce-0000000000000001",
    )
    with pytest.raises(MemoryProvenanceError) as exc:
        verify_memory_proxy_proof(
            proof,
            b"{}",
            method="POST",
            path="/v1/chat/completions",
            now=now,
        )
    assert exc.value.code == "proxy_replay_capacity_exceeded"
    persisted = json.loads(__import__("pathlib").Path(state_path).read_text())
    assert persisted == state
