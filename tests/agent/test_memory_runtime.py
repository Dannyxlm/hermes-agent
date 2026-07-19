from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nacl.signing import SigningKey

from agent.memory_protocol import (
    EXPECTED_PROTOCOL_BUNDLE_DIGEST,
    canonical_json_bytes,
    canonical_json_digest,
)
from agent.memory_provenance import issue_authenticated_ingress, mint_turn_provenance
from agent.memory_runtime import (
    CONFIG_ENV,
    CONFIG_SCHEMA_VERSION,
    DEPLOYMENT_MODE,
    POLICY_ENV,
    POLICY_SCHEMA_VERSION,
    PUBLIC_KEY_ENV,
    REPLAY_STATE_ENV,
    RUNTIME_MANIFEST_DIGEST_ENV,
    RUNTIME_MANIFEST_ENV,
    SNAPSHOT_ENV,
    SUBJECT_BINDINGS_ENV,
    MemoryRuntimeController,
)


NOW = datetime(2026, 7, 19, 12, 5, tzinfo=timezone.utc)
KEY_ID = "memory-release-key-1"
ISSUER = "cloudseed-memory-control"
SIGNING_KEY = SigningKey(b"\x31" * 32)
PUBLIC_KEY = bytes(SIGNING_KEY.verify_key)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write(path: Path, value, *, mode: int = 0o600) -> bytes:
    raw = value if isinstance(value, bytes) else json.dumps(value, separators=(",", ":")).encode()
    path.write_bytes(raw)
    path.chmod(mode)
    return raw


def _policy() -> dict:
    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "deployment_mode": DEPLOYMENT_MODE,
        "local_reply": True,
        "existing_memory_read": True,
        "memory_tools_visible": True,
        "governed_write": False,
        "conversational_capture": False,
        "provider_create": False,
    }


def _runtime_manifest(*, policy_digest: str, config_digest: str) -> dict:
    return {
        "schema_version": "memory-runtime-manifest/v3",
        "manifest_id": "runtime-hermes-memory-v3-test",
        "generated_at": _timestamp(NOW - timedelta(minutes=1)),
        "expires_at": _timestamp(NOW + timedelta(minutes=10)),
        "plan": {"id": "test-plan", "digest": "c" * 64},
        "release": {
            "generation": "memory-v3-readonly-core",
            "deployment_mode": DEPLOYMENT_MODE,
            "authorization": "none",
        },
        "protocol": {
            "bundle_digest": EXPECTED_PROTOCOL_BUNDLE_DIGEST,
            "protocol_version": 1,
            "supported_versions": [1],
            "minimum_version": 1,
            "policy_version": 1,
            "supported_policy_versions": [1],
            "minimum_policy_version": 1,
            "capability_version": 1,
            "supported_capability_versions": [1],
            "minimum_capability_version": 1,
            "security_epoch": 1,
        },
        "policy": {"digest": policy_digest, "write_mode": "deny"},
        "config": {"present": True, "digest": config_digest},
        "target": {
            "destination": "hermes-production",
            "audience": "hermes-agent",
            "capability_issuer": ISSUER,
            "capability_key_id": KEY_ID,
            "capability_public_key_digest": hashlib.sha256(PUBLIC_KEY).hexdigest(),
            "status_connection_env": "MEMORY_RELEASE_STATUS_DATABASE_URL",
            "access_precondition": "authenticated_reverse_proxy_only_direct_listener_unreachable",
        },
        "sources": [],
        "wrappers": [],
        "schedulers": [],
        "wiki": {"status": "unknown_verify", "evidence_revision": None, "evidence_digest": None},
        "qdrant": {"status": "unknown_verify", "pointer": None, "aliases": [], "watermark": None},
        "honcho": {"status": "unknown_verify", "capability_version": None, "resource_state_digest": None},
        "built_in_memory": {"status": "unknown_verify", "saturation": None, "state_digest": None},
        "services": [],
        "recovery": [],
        "units": [],
    }


def _snapshot(runtime: dict, *, issued_at: datetime | None = None) -> dict:
    issued = issued_at or (NOW - timedelta(minutes=1))
    payload = {
        "schema_version": "memory-capability-snapshot/v1",
        "snapshot_id": "pending",
        "issuer": ISSUER,
        "audience": "hermes-agent",
        "destination": "hermes-production",
        "deployment_mode": DEPLOYMENT_MODE,
        "security_epoch": 1,
        "minimum_protocol_version": 1,
        "minimum_policy_version": 1,
        "capability_version": 1,
        "runtime_manifest_digest": canonical_json_digest(runtime),
        "config_digest": runtime["config"]["digest"],
        "protocol_bundle_digest": EXPECTED_PROTOCOL_BUNDLE_DIGEST,
        "policy_digest": runtime["policy"]["digest"],
        "issued_at": _timestamp(issued),
        "expires_at": _timestamp(issued + timedelta(minutes=9)),
        "capabilities": {
            "local_reply": True,
            "existing_memory_read": True,
            "memory_tools_visible": True,
            "governed_write": False,
            "conversational_capture": False,
            "provider_create": False,
        },
    }
    identity = dict(payload)
    identity.pop("snapshot_id")
    payload["snapshot_id"] = f"cap-{hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24]}"
    return payload


def _envelope(snapshot: dict) -> dict:
    protected = {
        "algorithm": "Ed25519",
        "signature_encoding": "ed25519-raw",
        "key_id": KEY_ID,
    }
    signature = SIGNING_KEY.sign(
        canonical_json_bytes({"protected": protected, "snapshot": snapshot})
    ).signature
    return {
        "schema_version": "memory-capability-envelope/v1",
        "protected": protected,
        "snapshot": snapshot,
        "signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def _fixture(tmp_path: Path) -> tuple[dict[str, str], dict]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    tmp_path.chmod(0o700)
    bindings = {
        "schema_version": "hermes-memory-subject-bindings/v1",
        "bindings": [
            {"platform": "telegram", "principal_id": "owner-principal", "subject_id": "danny"}
        ],
    }
    bindings_path = tmp_path / "bindings.json"
    bindings_raw = _write(bindings_path, bindings)
    config = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "deployment_mode": DEPLOYMENT_MODE,
        "subject_id": "danny",
        "allowed_origins": [
            "telegram_private",
            "cli",
            "tui",
            "photon_api",
            "desktop_websocket",
        ],
        "subject_bindings_digest": hashlib.sha256(bindings_raw).hexdigest(),
        "provider": {
            "name": "honcho",
            "target": {
                "workspace_id": "ava-danny",
                "user_peer_id": "danny-user",
                "assistant_peer_id": "ava-assistant",
                "session_id": "ava-memory",
                "provider_base_url": "https://api.honcho.dev",
                "provider_environment": "production",
                "provider_host": "hermes",
            },
            "limits": {
                "deadline_seconds": 1.0,
                "max_provider_calls": 4,
                "max_items": 8,
                "max_chars": 4000,
            },
        },
    }
    policy = _policy()
    runtime = _runtime_manifest(
        policy_digest=canonical_json_digest(policy),
        config_digest=canonical_json_digest(config),
    )
    snapshot = _snapshot(runtime)

    config_path = tmp_path / "config.json"
    policy_path = tmp_path / "policy.json"
    runtime_path = tmp_path / "runtime.json"
    snapshot_path = tmp_path / "snapshot.json"
    key_path = tmp_path / "public.key"
    replay_path = tmp_path / "replay.json"
    _write(config_path, config)
    _write(policy_path, policy)
    _write(runtime_path, runtime)
    _write(snapshot_path, _envelope(snapshot))
    _write(key_path, PUBLIC_KEY)
    env = {
        CONFIG_ENV: str(config_path),
        POLICY_ENV: str(policy_path),
        RUNTIME_MANIFEST_ENV: str(runtime_path),
        RUNTIME_MANIFEST_DIGEST_ENV: canonical_json_digest(runtime),
        SNAPSHOT_ENV: str(snapshot_path),
        PUBLIC_KEY_ENV: str(key_path),
        REPLAY_STATE_ENV: str(replay_path),
        SUBJECT_BINDINGS_ENV: str(bindings_path),
    }
    return env, runtime


def _provenance(
    *,
    subject_id: str = "danny",
    origin: str = "telegram_private",
    authenticated_at: datetime = NOW,
):
    ingress = issue_authenticated_ingress(
        origin=origin,
        platform="telegram",
        principal_id="owner-principal",
        subject_id=subject_id,
        authenticated_at=authenticated_at,
    )
    return mint_turn_provenance(
        ingress,
        session_id="session-1",
        turn_id="turn-1",
        message_id="message-1",
    )


def test_runtime_is_opt_in_and_partial_activation_fails_closed(tmp_path: Path) -> None:
    assert MemoryRuntimeController({}).active is False

    config_path = tmp_path / "config.json"
    _write(config_path, {"schema_version": CONFIG_SCHEMA_VERSION})
    controller = MemoryRuntimeController({CONFIG_ENV: str(config_path)}, now=NOW)
    assert controller.active is True
    assert controller.bootstrap_failure_code == "memory_runtime_config_incomplete"
    assert controller.provider_capability_ceiling["existing_memory_read"] is False


def test_valid_snapshot_is_cached_for_multiple_turn_reads_without_replay(tmp_path: Path) -> None:
    env, _runtime = _fixture(tmp_path)
    controller = MemoryRuntimeController(env, now=NOW)
    assert controller.bootstrap_failure_code == ""
    assert controller.provider_name == "honcho"

    first = controller.authorize_explicit_read(
        memory_provenance=_provenance(), tool_call_id="tool-1", now=NOW
    )
    second = controller.authorize_explicit_read(
        memory_provenance=_provenance(), tool_call_id="tool-2", now=NOW
    )
    assert first.allowed is True
    assert second.allowed is True

    restarted = MemoryRuntimeController(env, now=NOW)
    replay = restarted.authorize_explicit_read(
        memory_provenance=_provenance(), tool_call_id="tool-3", now=NOW
    )
    assert replay.allowed is False
    assert replay.code == "snapshot_replay"


def test_snapshot_expiry_subject_origin_and_missing_tool_id_deny_before_read(tmp_path: Path) -> None:
    env, _runtime = _fixture(tmp_path)
    controller = MemoryRuntimeController(env, now=NOW)

    wrong_subject = controller.authorize_explicit_read(
        memory_provenance=_provenance(subject_id="someone-else"),
        tool_call_id="tool-subject",
        now=NOW,
    )
    group = controller.authorize_explicit_read(
        memory_provenance=_provenance(origin="telegram_group"),
        tool_call_id="tool-group",
        now=NOW,
    )
    missing_id = controller.authorize_explicit_read(
        memory_provenance=_provenance(), tool_call_id="", now=NOW
    )
    assert wrong_subject.code == "memory_subject_denied"
    assert group.code == "memory_origin_denied"
    assert missing_id.code == "memory_provenance_denied"

    assert controller.authorize_explicit_read(
        memory_provenance=_provenance(), tool_call_id="tool-ok", now=NOW
    ).allowed
    expired = controller.authorize_explicit_read(
        memory_provenance=_provenance(authenticated_at=NOW + timedelta(minutes=9)),
        tool_call_id="tool-expired",
        now=NOW + timedelta(minutes=9),
    )
    assert expired.allowed is False
    assert expired.code == "memory_snapshot_expired"


def test_subject_binding_drift_and_snapshot_unavailability_stay_local_only(tmp_path: Path) -> None:
    env, _runtime = _fixture(tmp_path)
    bindings_path = Path(env[SUBJECT_BINDINGS_ENV])
    changed = {
        "schema_version": "hermes-memory-subject-bindings/v1",
        "bindings": [
            {"platform": "telegram", "principal_id": "different", "subject_id": "danny"}
        ],
    }
    _write(bindings_path, changed)
    drifted = MemoryRuntimeController(env, now=NOW)
    assert drifted.bootstrap_failure_code == "subject_bindings_digest_mismatch"

    clean_env, _runtime = _fixture(tmp_path / "clean")
    os.unlink(clean_env[SNAPSHOT_ENV])
    missing = MemoryRuntimeController(clean_env, now=NOW).authorize_explicit_read(
        memory_provenance=_provenance(), tool_call_id="tool-missing", now=NOW
    )
    assert missing.allowed is False
    assert missing.code == "memory_snapshot_unavailable"


def test_post_boot_subject_binding_drift_and_stale_provenance_deny(tmp_path: Path) -> None:
    env, _runtime = _fixture(tmp_path)
    controller = MemoryRuntimeController(env, now=NOW)
    bindings_path = Path(env[SUBJECT_BINDINGS_ENV])
    changed = {
        "schema_version": "hermes-memory-subject-bindings/v1",
        "bindings": [
            {"platform": "telegram", "principal_id": "new-principal", "subject_id": "danny"}
        ],
    }
    _write(bindings_path, changed)
    drifted = controller.authorize_explicit_read(
        memory_provenance=_provenance(),
        tool_call_id="tool-drift",
        now=NOW,
    )
    assert drifted.allowed is False
    assert drifted.code == "subject_bindings_digest_mismatch"

    fresh_env, _runtime = _fixture(tmp_path / "fresh")
    fresh = MemoryRuntimeController(fresh_env, now=NOW)
    stale = fresh.authorize_explicit_read(
        memory_provenance=_provenance(
            authenticated_at=NOW - timedelta(minutes=6)
        ),
        tool_call_id="tool-stale",
        now=NOW,
    )
    assert stale.allowed is False
    assert stale.code == "memory_provenance_stale"
