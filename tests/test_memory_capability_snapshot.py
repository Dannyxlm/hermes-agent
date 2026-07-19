from __future__ import annotations

import base64
import errno
import hashlib
import json
import multiprocessing
import os
import shutil
import subprocess
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from agent.memory_capability_snapshot import (
    CapabilityVerificationError,
    DurableSnapshotReplayGuard,
    MemoryCapabilityVerifier,
    ReplayGuard,
    SSHSIG_NAMESPACE,
    SnapshotReplayGuard,
)
from agent.memory_protocol import (
    EXPECTED_PROTOCOL_BUNDLE_DIGEST,
    canonical_json_bytes,
    canonical_json_digest,
)


NOW = datetime(2026, 7, 19, 12, 5, tzinfo=timezone.utc)
POLICY_DIGEST = "a" * 64
CONFIG_DIGEST = "b" * 64
KEY_ID = "memory-release-key-1"
ISSUER = "cloudseed-memory-control"
DEFAULT_SIGNING_KEY = SigningKey(b"\x11" * 32)
DEFAULT_PUBLIC_KEY = bytes(DEFAULT_SIGNING_KEY.verify_key)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def runtime_manifest(public_key: bytes, **overrides) -> dict:
    payload = {
        "schema_version": "memory-runtime-manifest/v3",
        "manifest_id": "runtime-hermes-capability-test",
        "generated_at": _timestamp(NOW - timedelta(minutes=1)),
        "expires_at": _timestamp(NOW + timedelta(minutes=10)),
        "plan": {"id": "test-plan", "digest": "c" * 64},
        "release": {
            "generation": "memory-v3-readonly-core",
            "deployment_mode": "tools_only_read_containment",
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
        "policy": {"digest": POLICY_DIGEST, "write_mode": "deny"},
        "config": {"present": True, "digest": CONFIG_DIGEST},
        "target": {
            "destination": "hermes-production",
            "audience": "hermes-agent",
            "capability_issuer": ISSUER,
            "capability_key_id": KEY_ID,
            "capability_public_key_digest": hashlib.sha256(public_key).hexdigest(),
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
    for dotted, value in overrides.items():
        section, field = dotted.split("__", 1)
        payload[section][field] = value
    return payload


def snapshot(runtime: dict | None = None, **overrides) -> dict:
    bound_runtime = runtime or runtime_manifest(DEFAULT_PUBLIC_KEY)
    payload = {
        "schema_version": "memory-capability-snapshot/v1",
        "snapshot_id": "pending",
        "issuer": ISSUER,
        "audience": "hermes-agent",
        "destination": "hermes-production",
        "deployment_mode": "tools_only_read_containment",
        "security_epoch": 1,
        "minimum_protocol_version": 1,
        "minimum_policy_version": 1,
        "capability_version": 1,
        "runtime_manifest_digest": canonical_json_digest(bound_runtime),
        "config_digest": bound_runtime["config"]["digest"],
        "protocol_bundle_digest": bound_runtime["protocol"]["bundle_digest"],
        "policy_digest": bound_runtime["policy"]["digest"],
        "issued_at": _timestamp(NOW - timedelta(minutes=1)),
        "expires_at": _timestamp(NOW + timedelta(minutes=9)),
        "capabilities": {
            "local_reply": True,
            "existing_memory_read": True,
            "memory_tools_visible": True,
            "governed_write": False,
            "conversational_capture": False,
            "provider_create": False,
        },
    }
    payload.update(overrides)
    identity = dict(payload)
    identity.pop("snapshot_id")
    payload["snapshot_id"] = f"cap-{hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24]}"
    return payload


def raw_envelope(signing_key: SigningKey, payload: dict | None = None, **protected_overrides) -> dict:
    protected = {
        "algorithm": "Ed25519",
        "signature_encoding": "ed25519-raw",
        "key_id": KEY_ID,
    }
    protected.update(protected_overrides)
    body = payload if payload is not None else snapshot()
    signature = signing_key.sign(
        canonical_json_bytes({"protected": protected, "snapshot": body})
    ).signature
    return {
        "schema_version": "memory-capability-envelope/v1",
        "protected": protected,
        "snapshot": body,
        "signature": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("="),
    }


def verifier(
    public_key: bytes,
    *,
    guard: ReplayGuard | None = None,
    manifest: dict | None = None,
    observed_policy_digest: str = POLICY_DIGEST,
    observed_config_digest: str = CONFIG_DIGEST,
) -> MemoryCapabilityVerifier:
    runtime = manifest or runtime_manifest(public_key)
    return MemoryCapabilityVerifier.from_frozen_runtime_manifest(
        runtime,
        expected_manifest_digest=canonical_json_digest(runtime),
        observed_policy_digest=observed_policy_digest,
        observed_config_digest=observed_config_digest,
        public_key=public_key,
        now=NOW,
        replay_guard=guard or SnapshotReplayGuard(),
    )


@pytest.fixture
def raw_keypair() -> tuple[SigningKey, bytes]:
    return DEFAULT_SIGNING_KEY, DEFAULT_PUBLIC_KEY


def assert_local_only(decision, failure_code: str) -> None:
    assert decision.outcome == "local_only"
    assert decision.failure_code == failure_code
    assert decision.local_reply_allowed is True
    assert decision.memory_allowed is False
    assert decision.snapshot is None


def _durable_verify_worker(state_path: str, envelope: dict, start, results) -> None:
    runtime = runtime_manifest(DEFAULT_PUBLIC_KEY)
    start.wait()
    decision = verifier(
        DEFAULT_PUBLIC_KEY,
        guard=DurableSnapshotReplayGuard(state_path),
        manifest=runtime,
    ).verify(envelope, now=NOW)
    results.put((decision.outcome, decision.failure_code))


def _hold_replay_lock(lock_path: str, ready, release) -> None:
    import fcntl

    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        ready.set()
        release.wait(timeout=10)
    finally:
        os.close(descriptor)


def test_raw_ed25519_equal_authenticated_versions_and_epoch_are_allowed(raw_keypair) -> None:
    signing_key, public_key = raw_keypair
    equal = verifier(public_key).verify(raw_envelope(signing_key), now=NOW)
    assert equal.memory_allowed is True
    assert equal.local_reply_allowed is True
    assert equal.snapshot is not None
    assert equal.snapshot.capabilities.existing_memory_read is True
    assert equal.snapshot.capabilities.governed_write is False
    assert equal.policy_digest == POLICY_DIGEST
    assert equal.config_digest == CONFIG_DIGEST


@pytest.mark.parametrize(
    ("field", "failure_code"),
    [
        ("security_epoch", "security_epoch_downgrade"),
        ("minimum_protocol_version", "protocol_version_downgrade"),
        ("minimum_policy_version", "policy_version_downgrade"),
        ("capability_version", "capability_version_downgrade"),
    ],
)
def test_each_numeric_floor_downgrade_is_denied(raw_keypair, field: str, failure_code: str) -> None:
    signing_key, public_key = raw_keypair
    payload = snapshot(**{field: 0})
    decision = verifier(public_key).verify(raw_envelope(signing_key, payload), now=NOW)
    assert_local_only(decision, failure_code)


@pytest.mark.parametrize(
    ("field", "failure_code"),
    [
        ("minimum_protocol_version", "protocol_version_unsupported"),
        ("minimum_policy_version", "policy_version_unsupported"),
        ("capability_version", "capability_version_unsupported"),
    ],
)
def test_future_unsupported_versions_are_denied(
    raw_keypair,
    field: str,
    failure_code: str,
) -> None:
    signing_key, public_key = raw_keypair
    decision = verifier(public_key).verify(
        raw_envelope(signing_key, snapshot(**{field: 999})),
        now=NOW,
    )
    assert_local_only(decision, failure_code)


@pytest.mark.parametrize("future_epoch", [2, 999])
def test_future_epoch_is_denied_without_poisoning_durable_watermark(
    tmp_path,
    raw_keypair,
    future_epoch: int,
) -> None:
    signing_key, public_key = raw_keypair
    runtime = runtime_manifest(public_key)
    state_path = tmp_path / "capability-replay.json"
    accepted = raw_envelope(signing_key, snapshot(runtime))
    assert verifier(
        public_key,
        guard=DurableSnapshotReplayGuard(state_path),
        manifest=runtime,
    ).verify(accepted, now=NOW).memory_allowed
    before = state_path.read_bytes()

    poisoned = snapshot(
        runtime,
        security_epoch=future_epoch,
        issued_at=_timestamp(NOW - timedelta(seconds=30)),
        expires_at=_timestamp(NOW + timedelta(minutes=9)),
    )
    decision = verifier(
        public_key,
        guard=DurableSnapshotReplayGuard(state_path),
        manifest=runtime,
    ).verify(raw_envelope(signing_key, poisoned), now=NOW)
    assert_local_only(decision, "security_epoch_unsupported")
    assert state_path.read_bytes() == before


@pytest.mark.parametrize(
    ("mutation", "failure_code"),
    [
        ({"schema_version": "memory-capability-snapshot/v2"}, "snapshot_schema_mismatch"),
        ({"issuer": "different-issuer"}, "issuer_mismatch"),
        ({"destination": "hermes-staging"}, "destination_mismatch"),
        ({"audience": "different-agent"}, "audience_mismatch"),
        ({"deployment_mode": "local_only"}, "deployment_mode_mismatch"),
        ({"runtime_manifest_digest": "f" * 64}, "runtime_manifest_digest_mismatch"),
        ({"config_digest": "f" * 64}, "config_digest_mismatch"),
        ({"protocol_bundle_digest": "f" * 64}, "protocol_bundle_digest_mismatch"),
        ({"policy_digest": "f" * 64}, "policy_digest_mismatch"),
    ],
)
def test_signed_schema_target_audience_mode_and_policy_mismatches_are_denied(
    raw_keypair,
    mutation: dict,
    failure_code: str,
) -> None:
    signing_key, public_key = raw_keypair
    decision = verifier(public_key).verify(
        raw_envelope(signing_key, snapshot(**mutation)),
        now=NOW,
    )
    assert_local_only(decision, failure_code)


def test_unknown_snapshot_field_is_denied_by_closed_world_parser(raw_keypair) -> None:
    signing_key, public_key = raw_keypair
    payload = snapshot()
    payload["ambient_override"] = True
    decision = verifier(public_key).verify(raw_envelope(signing_key, payload), now=NOW)
    assert_local_only(decision, "schema_mismatch")


def test_envelope_schema_snapshot_identity_and_write_enablement_are_denied(raw_keypair) -> None:
    signing_key, public_key = raw_keypair
    envelope = raw_envelope(signing_key)
    envelope["schema_version"] = "memory-capability-envelope/v2"
    assert_local_only(
        verifier(public_key).verify(envelope, now=NOW),
        "envelope_schema_mismatch",
    )

    forged_identity = snapshot()
    forged_identity["snapshot_id"] = "cap-forged"
    assert_local_only(
        verifier(public_key).verify(raw_envelope(signing_key, forged_identity), now=NOW),
        "snapshot_identity_mismatch",
    )

    writes = snapshot()
    writes["capabilities"]["governed_write"] = True
    identity = dict(writes)
    identity.pop("snapshot_id")
    writes["snapshot_id"] = f"cap-{hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24]}"
    assert_local_only(
        verifier(public_key).verify(raw_envelope(signing_key, writes), now=NOW),
        "write_capability_forbidden",
    )


@pytest.mark.parametrize(
    ("issued_at", "expires_at", "failure_code"),
    [
        (NOW + timedelta(seconds=1), NOW + timedelta(minutes=5), "snapshot_not_yet_valid"),
        (NOW - timedelta(minutes=10), NOW, "snapshot_expired"),
    ],
)
def test_not_before_and_expiry_fail_to_local_only(
    raw_keypair,
    issued_at: datetime,
    expires_at: datetime,
    failure_code: str,
) -> None:
    signing_key, public_key = raw_keypair
    payload = snapshot(issued_at=_timestamp(issued_at), expires_at=_timestamp(expires_at))
    decision = verifier(public_key).verify(raw_envelope(signing_key, payload), now=NOW)
    assert_local_only(decision, failure_code)


@pytest.mark.parametrize(
    ("protected", "failure_code"),
    [
        ({"key_id": "old-release-key"}, "key_identity_mismatch"),
        ({"algorithm": "Ed448"}, "signature_algorithm_mismatch"),
        ({"signature_encoding": "hex"}, "signature_encoding_mismatch"),
    ],
)
def test_key_algorithm_and_encoding_mismatches_are_denied(
    raw_keypair,
    protected: dict,
    failure_code: str,
) -> None:
    signing_key, public_key = raw_keypair
    decision = verifier(public_key).verify(
        raw_envelope(signing_key, **protected),
        now=NOW,
    )
    assert_local_only(decision, failure_code)


def test_tampered_raw_signature_and_noncanonical_base64_are_denied(raw_keypair) -> None:
    signing_key, public_key = raw_keypair
    envelope = raw_envelope(signing_key)
    signature = base64.urlsafe_b64decode(envelope["signature"] + "==")
    envelope["signature"] = base64.urlsafe_b64encode(
        bytes([signature[0] ^ 1]) + signature[1:]
    ).decode("ascii").rstrip("=")
    assert_local_only(verifier(public_key).verify(envelope, now=NOW), "signature_invalid")

    envelope = raw_envelope(signing_key)
    envelope["signature"] = "a"
    assert_local_only(
        verifier(public_key).verify(envelope, now=NOW),
        "signature_encoding_invalid",
    )


def test_replay_and_valid_old_snapshot_are_monotonically_denied(raw_keypair) -> None:
    signing_key, public_key = raw_keypair
    guard = SnapshotReplayGuard()
    shared = verifier(public_key, guard=guard)
    current = raw_envelope(signing_key)
    assert shared.verify(current, now=NOW).memory_allowed is True
    assert_local_only(shared.verify(current, now=NOW), "snapshot_replay")

    later_payload = snapshot(
        issued_at=_timestamp(NOW - timedelta(seconds=15)),
        expires_at=_timestamp(NOW + timedelta(minutes=9)),
    )
    older_payload = snapshot(
        issued_at=_timestamp(NOW - timedelta(minutes=2)),
        expires_at=_timestamp(NOW + timedelta(minutes=8)),
    )
    ordered_guard = SnapshotReplayGuard()
    ordered = verifier(public_key, guard=ordered_guard)
    assert ordered.verify(raw_envelope(signing_key, later_payload), now=NOW).memory_allowed
    assert_local_only(
        ordered.verify(raw_envelope(signing_key, older_payload), now=NOW),
        "valid_old_snapshot",
    )


def test_durable_replay_guard_survives_restart_and_rejects_valid_old(tmp_path, raw_keypair) -> None:
    signing_key, public_key = raw_keypair
    runtime = runtime_manifest(public_key)
    state_path = tmp_path / "capability-replay.json"
    current = raw_envelope(signing_key, snapshot(runtime))

    first = verifier(
        public_key,
        guard=DurableSnapshotReplayGuard(state_path),
        manifest=runtime,
    )
    assert first.verify(current, now=NOW).memory_allowed is True
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert Path(f"{state_path}.lock").stat().st_mode & 0o777 == 0o600
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["schema_version"] == "memory-capability-replay/v1"
    assert state["state_version"] == 1

    restarted = verifier(
        public_key,
        guard=DurableSnapshotReplayGuard(state_path),
        manifest=runtime,
    )
    assert_local_only(restarted.verify(current, now=NOW), "snapshot_replay")

    newer = snapshot(
        runtime,
        issued_at=_timestamp(NOW - timedelta(seconds=30)),
        expires_at=_timestamp(NOW + timedelta(minutes=9)),
    )
    assert verifier(
        public_key,
        guard=DurableSnapshotReplayGuard(state_path),
        manifest=runtime,
    ).verify(raw_envelope(signing_key, newer), now=NOW).memory_allowed

    older = snapshot(
        runtime,
        issued_at=_timestamp(NOW - timedelta(minutes=2)),
        expires_at=_timestamp(NOW + timedelta(minutes=8)),
    )
    assert_local_only(
        verifier(
            public_key,
            guard=DurableSnapshotReplayGuard(state_path),
            manifest=runtime,
        ).verify(raw_envelope(signing_key, older), now=NOW),
        "valid_old_snapshot",
    )
    assert json.loads(state_path.read_text(encoding="utf-8"))["state_version"] == 2


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="fork is unavailable")
def test_durable_replay_guard_serializes_concurrent_processes(tmp_path, raw_keypair) -> None:
    signing_key, _public_key = raw_keypair
    envelope = raw_envelope(signing_key)
    state_path = tmp_path / "capability-replay.json"
    context = multiprocessing.get_context("fork")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(
            target=_durable_verify_worker,
            args=(str(state_path), envelope, start, results),
        )
        for _ in range(8)
    ]
    for worker in workers:
        worker.start()
    start.set()
    outcomes = [results.get(timeout=10) for _ in workers]
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0
    assert outcomes.count(("allow", None)) == 1
    assert outcomes.count(("local_only", "snapshot_replay")) == 7


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable")
@pytest.mark.parametrize("fifo_target", ["state", "lock"])
def test_replay_fifo_substitution_fails_closed_without_blocking(
    tmp_path,
    raw_keypair,
    fifo_target: str,
) -> None:
    signing_key, public_key = raw_keypair
    state_path = tmp_path / "capability-replay.json"
    fifo_path = state_path if fifo_target == "state" else Path(f"{state_path}.lock")
    os.mkfifo(fifo_path, 0o600)

    started = time.monotonic()
    decision = verifier(
        public_key,
        guard=DurableSnapshotReplayGuard(state_path),
    ).verify(raw_envelope(signing_key), now=NOW)
    elapsed = time.monotonic() - started

    assert_local_only(decision, "replay_state_path_invalid")
    assert elapsed < 1.0


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable")
def test_runtime_and_key_paths_are_never_opened_as_implicit_inputs(tmp_path, raw_keypair) -> None:
    signing_key, public_key = raw_keypair
    runtime = runtime_manifest(public_key)
    runtime_fifo = tmp_path / "runtime.json"
    key_fifo = tmp_path / "capability.pub"
    os.mkfifo(runtime_fifo, 0o600)
    os.mkfifo(key_fifo, 0o600)

    started = time.monotonic()
    runtime_path = MemoryCapabilityVerifier.from_frozen_runtime_manifest(
        runtime_fifo,
        expected_manifest_digest="a" * 64,
        observed_policy_digest=POLICY_DIGEST,
        observed_config_digest=CONFIG_DIGEST,
        public_key=public_key,
        now=NOW,
        replay_guard=SnapshotReplayGuard(),
    )
    key_path = MemoryCapabilityVerifier.from_frozen_runtime_manifest(
        runtime,
        expected_manifest_digest=canonical_json_digest(runtime),
        observed_policy_digest=POLICY_DIGEST,
        observed_config_digest=CONFIG_DIGEST,
        public_key=key_fifo,
        now=NOW,
        replay_guard=SnapshotReplayGuard(),
    )
    elapsed = time.monotonic() - started

    assert_local_only(runtime_path.verify(raw_envelope(signing_key), now=NOW), "invalid_json")
    assert_local_only(key_path.verify(raw_envelope(signing_key), now=NOW), "public_key_invalid")
    assert elapsed < 1.0


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="fork is unavailable")
def test_held_cross_process_replay_lock_has_a_bounded_typed_denial(
    tmp_path,
    raw_keypair,
) -> None:
    signing_key, public_key = raw_keypair
    state_path = tmp_path / "capability-replay.json"
    lock_path = f"{state_path}.lock"
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(target=_hold_replay_lock, args=(lock_path, ready, release))
    holder.start()
    try:
        assert ready.wait(timeout=5)
        started = time.monotonic()
        decision = verifier(
            public_key,
            guard=DurableSnapshotReplayGuard(state_path),
        ).verify(raw_envelope(signing_key), now=NOW)
        elapsed = time.monotonic() - started
        assert_local_only(decision, "replay_guard_contended")
        assert elapsed < 1.5
    finally:
        release.set()
        holder.join(timeout=5)
    assert holder.exitcode == 0


def test_durable_watermark_survives_issuer_and_key_rotation(tmp_path, raw_keypair) -> None:
    signing_key, public_key = raw_keypair
    state_path = tmp_path / "capability-replay.json"
    initial_runtime = runtime_manifest(public_key)
    initial = raw_envelope(signing_key, snapshot(initial_runtime))
    assert verifier(
        public_key,
        guard=DurableSnapshotReplayGuard(state_path),
        manifest=initial_runtime,
    ).verify(initial, now=NOW).memory_allowed

    rotated_signing_key = SigningKey(b"\x22" * 32)
    rotated_public_key = bytes(rotated_signing_key.verify_key)
    rotated_runtime = runtime_manifest(
        rotated_public_key,
        target__capability_issuer="cloudseed-memory-control-rotated",
        target__capability_key_id="memory-release-key-2",
    )
    same_age = snapshot(
        rotated_runtime,
        issuer="cloudseed-memory-control-rotated",
    )
    rotated_envelope = raw_envelope(
        rotated_signing_key,
        same_age,
        key_id="memory-release-key-2",
    )
    assert_local_only(
        verifier(
            rotated_public_key,
            guard=DurableSnapshotReplayGuard(state_path),
            manifest=rotated_runtime,
        ).verify(rotated_envelope, now=NOW),
        "valid_old_snapshot",
    )

    newer = snapshot(
        rotated_runtime,
        issuer="cloudseed-memory-control-rotated",
        issued_at=_timestamp(NOW - timedelta(seconds=30)),
        expires_at=_timestamp(NOW + timedelta(minutes=9)),
    )
    assert verifier(
        rotated_public_key,
        guard=DurableSnapshotReplayGuard(state_path),
        manifest=rotated_runtime,
    ).verify(
        raw_envelope(rotated_signing_key, newer, key_id="memory-release-key-2"),
        now=NOW,
    ).memory_allowed


@pytest.mark.parametrize("contents", [b"{", b"x" * (256 * 1024 + 1)])
def test_corrupt_or_oversized_replay_state_fails_to_local_only(
    tmp_path,
    raw_keypair,
    contents: bytes,
) -> None:
    signing_key, public_key = raw_keypair
    state_path = tmp_path / "capability-replay.json"
    state_path.write_bytes(contents)
    state_path.chmod(0o600)
    decision = verifier(
        public_key,
        guard=DurableSnapshotReplayGuard(state_path),
    ).verify(raw_envelope(signing_key), now=NOW)
    assert_local_only(decision, "replay_state_corrupt")


def test_symlinked_replay_state_and_lock_fail_to_local_only(tmp_path, raw_keypair) -> None:
    signing_key, public_key = raw_keypair
    target = tmp_path / "target"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o600)

    state_path = tmp_path / "linked-replay.json"
    state_path.symlink_to(target)
    decision = verifier(
        public_key,
        guard=DurableSnapshotReplayGuard(state_path),
    ).verify(raw_envelope(signing_key), now=NOW)
    assert_local_only(decision, "replay_state_path_invalid")

    other_state = tmp_path / "other-replay.json"
    lock_path = Path(f"{other_state}.lock")
    lock_path.symlink_to(target)
    decision = verifier(
        public_key,
        guard=DurableSnapshotReplayGuard(other_state),
    ).verify(raw_envelope(signing_key), now=NOW)
    assert_local_only(decision, "replay_guard_unavailable")


def test_non_private_or_non_regular_replay_state_fails_to_local_only(tmp_path, raw_keypair) -> None:
    signing_key, public_key = raw_keypair
    insecure = tmp_path / "insecure-replay.json"
    insecure.write_text(
        json.dumps(
            {
                "schema_version": "memory-capability-replay/v1",
                "state_version": 1,
                "watermarks": [],
            }
        ),
        encoding="utf-8",
    )
    insecure.chmod(0o644)
    decision = verifier(
        public_key,
        guard=DurableSnapshotReplayGuard(insecure),
    ).verify(raw_envelope(signing_key), now=NOW)
    assert_local_only(decision, "replay_state_path_invalid")

    directory = tmp_path / "directory-replay.json"
    directory.mkdir()
    decision = verifier(
        public_key,
        guard=DurableSnapshotReplayGuard(directory),
    ).verify(raw_envelope(signing_key), now=NOW)
    assert_local_only(decision, "replay_state_path_invalid")


def test_unavailable_parent_and_unavailable_kernel_lock_fail_to_local_only(
    tmp_path,
    raw_keypair,
    monkeypatch,
) -> None:
    signing_key, public_key = raw_keypair
    missing_parent = tmp_path / "missing" / "capability-replay.json"
    decision = verifier(
        public_key,
        guard=DurableSnapshotReplayGuard(missing_parent),
    ).verify(raw_envelope(signing_key), now=NOW)
    assert_local_only(decision, "replay_state_path_unavailable")

    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir(mode=0o777)
    unsafe_parent.chmod(0o777)
    decision = verifier(
        public_key,
        guard=DurableSnapshotReplayGuard(unsafe_parent / "capability-replay.json"),
    ).verify(raw_envelope(signing_key), now=NOW)
    assert_local_only(decision, "replay_state_path_unavailable")

    import agent.memory_capability_snapshot as capability_module

    real_fcntl = capability_module._fcntl
    monkeypatch.setattr(capability_module, "_fcntl", None)
    decision = verifier(
        public_key,
        guard=DurableSnapshotReplayGuard(tmp_path / "unlocked.json"),
    ).verify(raw_envelope(signing_key), now=NOW)
    assert_local_only(decision, "replay_guard_lock_unavailable")

    class InterruptedFcntl:
        LOCK_EX = real_fcntl.LOCK_EX
        LOCK_NB = real_fcntl.LOCK_NB
        LOCK_UN = real_fcntl.LOCK_UN

        @staticmethod
        def flock(_descriptor, _operation):
            raise OSError(errno.EINTR, "interrupted")

    monkeypatch.setattr(capability_module, "_fcntl", InterruptedFcntl)
    monkeypatch.setattr(capability_module, "REPLAY_LOCK_TIMEOUT_SECONDS", 0.03)
    started = time.monotonic()
    decision = verifier(
        public_key,
        guard=DurableSnapshotReplayGuard(tmp_path / "interrupted.json"),
    ).verify(raw_envelope(signing_key), now=NOW)
    elapsed = time.monotonic() - started
    assert_local_only(decision, "replay_guard_lock_unavailable")
    assert elapsed < 0.5


def test_durable_replay_scope_capacity_returns_typed_local_only(
    tmp_path,
    raw_keypair,
    monkeypatch,
) -> None:
    signing_key, public_key = raw_keypair
    state_path = tmp_path / "capability-replay.json"

    import agent.memory_capability_snapshot as capability_module

    monkeypatch.setattr(capability_module, "MAX_REPLAY_SCOPES", 1)
    first_runtime = runtime_manifest(public_key)
    first = raw_envelope(signing_key, snapshot(first_runtime))
    assert verifier(
        public_key,
        guard=DurableSnapshotReplayGuard(state_path),
        manifest=first_runtime,
    ).verify(first, now=NOW).memory_allowed

    second_runtime = runtime_manifest(
        public_key,
        target__destination="hermes-secondary",
    )
    second_snapshot = snapshot(
        second_runtime,
        destination="hermes-secondary",
        issued_at=_timestamp(NOW - timedelta(seconds=30)),
        expires_at=_timestamp(NOW + timedelta(minutes=9)),
    )
    decision = verifier(
        public_key,
        guard=DurableSnapshotReplayGuard(state_path),
        manifest=second_runtime,
    ).verify(raw_envelope(signing_key, second_snapshot), now=NOW)
    assert_local_only(decision, "replay_state_capacity_exceeded")


def test_newer_issued_snapshot_cannot_weaken_an_already_accepted_floor(raw_keypair) -> None:
    signing_key, public_key = raw_keypair
    verified = verifier(public_key).verify(raw_envelope(signing_key), now=NOW)
    assert verified.snapshot is not None
    guard = SnapshotReplayGuard()
    stronger = replace(
        verified.snapshot,
        snapshot_id="cap-stronger-watermark",
        security_epoch=2,
        issued_at=NOW - timedelta(minutes=2),
    )
    weaker = replace(
        verified.snapshot,
        snapshot_id="cap-weaker-watermark",
        security_epoch=1,
        issued_at=NOW - timedelta(minutes=1),
    )
    guard.claim(stronger, key_id=KEY_ID)
    with pytest.raises(CapabilityVerificationError) as error:
        guard.claim(weaker, key_id=KEY_ID)
    assert error.value.code == "monotonic_floor_downgrade"


def test_policy_config_manifest_and_public_key_binding_fail_without_raising(raw_keypair) -> None:
    signing_key, public_key = raw_keypair
    envelope = raw_envelope(signing_key)

    config_drift = verifier(public_key, observed_config_digest="f" * 64)
    assert_local_only(config_drift.verify(envelope, now=NOW), "config_digest_mismatch")

    policy_drift = verifier(public_key, observed_policy_digest="f" * 64)
    assert_local_only(policy_drift.verify(envelope, now=NOW), "policy_digest_mismatch")

    different_key = bytes(SigningKey(b"\x22" * 32).verify_key)
    runtime = runtime_manifest(public_key)
    key_drift = MemoryCapabilityVerifier.from_frozen_runtime_manifest(
        runtime,
        expected_manifest_digest=canonical_json_digest(runtime),
        observed_policy_digest=POLICY_DIGEST,
        observed_config_digest=CONFIG_DIGEST,
        public_key=different_key,
        now=NOW,
        replay_guard=SnapshotReplayGuard(),
    )
    assert_local_only(key_drift.verify(envelope, now=NOW), "public_key_digest_mismatch")


def test_verifier_requires_an_explicit_replay_guard(raw_keypair) -> None:
    signing_key, public_key = raw_keypair
    runtime = runtime_manifest(public_key)
    missing_guard = MemoryCapabilityVerifier.from_frozen_runtime_manifest(
        runtime,
        expected_manifest_digest=canonical_json_digest(runtime),
        observed_policy_digest=POLICY_DIGEST,
        observed_config_digest=CONFIG_DIGEST,
        public_key=public_key,
        now=NOW,
        replay_guard=None,
    )
    assert_local_only(
        missing_guard.verify(raw_envelope(signing_key, snapshot(runtime)), now=NOW),
        "replay_guard_required",
    )


def test_denial_payload_is_typed_and_keeps_local_reply_available(raw_keypair) -> None:
    signing_key, public_key = raw_keypair
    decision = verifier(public_key).verify(
        raw_envelope(signing_key, snapshot(destination="wrong-target")),
        now=NOW,
    )
    denial = decision.to_denial_payload(action="existing_memory_read", created_at=NOW)
    assert set(denial) == {
        "schema_version",
        "denial_id",
        "reason_code",
        "action",
        "local_reply_allowed",
        "retryable",
        "created_at",
    }
    assert denial["schema_version"] == "memory-denial/v1"
    assert denial["local_reply_allowed"] is True
    assert decision.to_denial_payload(action="a" * 100, created_at=NOW)["action"] == "a" * 100

    class ActionSubclass(str):
        pass

    for unsafe_action in (
        None,
        7,
        "",
        "a" * 101,
        "raw message contents",
        "../existing_memory_read",
        "ExistingMemoryRead",
        "existing-memory-read",
        "existing_memory_read\nprivate text",
        "existing_memory_read_",
        ActionSubclass("existing_memory_read"),
    ):
        with pytest.raises(ValueError, match="stable"):
            decision.to_denial_payload(action=unsafe_action, created_at=NOW)


def _sshsig_envelope(private_key: Path, payload: dict) -> dict:
    protected = {
        "algorithm": "Ed25519",
        "signature_encoding": "sshsig",
        "key_id": KEY_ID,
    }
    message = canonical_json_bytes({"protected": protected, "snapshot": payload})
    message_path = private_key.parent / "snapshot.json"
    message_path.write_bytes(message)
    subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "sign",
            "-f",
            str(private_key),
            "-n",
            SSHSIG_NAMESPACE,
            str(message_path),
        ],
        check=True,
        capture_output=True,
    )
    signature = Path(f"{message_path}.sig").read_bytes()
    return {
        "schema_version": "memory-capability-envelope/v1",
        "protected": protected,
        "snapshot": payload,
        "signature": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("="),
    }


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is unavailable")
def test_sshsig_valid_and_tampered_envelopes(tmp_path) -> None:
    private_key = tmp_path / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private_key)],
        check=True,
        capture_output=True,
    )
    public_key = private_key.with_suffix(".pub").read_bytes()
    runtime = runtime_manifest(public_key)
    envelope = _sshsig_envelope(private_key, snapshot(runtime))

    accepted = verifier(public_key, manifest=runtime).verify(envelope, now=NOW)
    assert accepted.memory_allowed is True

    signature = bytearray(base64.urlsafe_b64decode(envelope["signature"] + "=" * (-len(envelope["signature"]) % 4)))
    signature[len(signature) // 2] ^= 1
    envelope["signature"] = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    assert_local_only(
        verifier(public_key, manifest=runtime).verify(envelope, now=NOW),
        "signature_invalid",
    )
