from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

import pytest

from agent.memory_protocol import (
    EXPECTED_PROTOCOL_BUNDLE_DIGEST,
    MAX_JSON_BYTES,
    MAX_JSON_DEPTH,
    MAX_JSON_INTEGER,
    MAX_JSON_NODES,
    MemoryProtocolError,
    PROTOCOL_SOURCE_COMMIT,
    VENDORED_PROTOCOL_ROOT,
    canonical_json_digest,
    load_memory_runtime_binding,
    strict_json_loads,
    verify_vendored_protocol_bundle,
)


NOW = datetime(2026, 7, 19, 12, 5, tzinfo=timezone.utc)
POLICY_DIGEST = "a" * 64
CONFIG_DIGEST = "b" * 64


class ExplodingMapping(Mapping):
    def __getitem__(self, _key):
        raise AssertionError("custom mapping was evaluated")

    def __iter__(self):
        raise AssertionError("custom mapping was evaluated")

    def __len__(self):
        raise AssertionError("custom mapping was evaluated")


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def runtime_manifest(public_key: bytes = b"k" * 32, **protocol_overrides) -> dict:
    protocol = {
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
    }
    protocol.update(protocol_overrides)
    return {
        "schema_version": "memory-runtime-manifest/v3",
        "manifest_id": "runtime-hermes-protocol-test",
        "generated_at": _timestamp(NOW - timedelta(minutes=1)),
        "expires_at": _timestamp(NOW + timedelta(minutes=10)),
        "plan": {"id": "test-plan", "digest": "c" * 64},
        "release": {
            "generation": "memory-v3-readonly-core",
            "deployment_mode": "tools_only_read_containment",
            "authorization": "none",
        },
        "protocol": protocol,
        "policy": {"digest": POLICY_DIGEST, "write_mode": "deny"},
        "config": {"present": True, "digest": CONFIG_DIGEST},
        "target": {
            "destination": "hermes-production",
            "audience": "hermes-agent",
            "capability_issuer": "cloudseed-memory-control",
            "capability_key_id": "memory-release-key-1",
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


def load(payload: dict, **overrides):
    arguments = {
        "expected_manifest_digest": canonical_json_digest(payload),
        "observed_policy_digest": POLICY_DIGEST,
        "observed_config_digest": CONFIG_DIGEST,
        "now": NOW,
    }
    arguments.update(overrides)
    return load_memory_runtime_binding(payload, **arguments)


def test_exact_exported_bundle_verifies_and_uses_frozen_numeric_floors() -> None:
    bundle = verify_vendored_protocol_bundle()

    assert bundle.bundle_digest == EXPECTED_PROTOCOL_BUNDLE_DIGEST
    assert PROTOCOL_SOURCE_COMMIT == "a588d2ccd5a0354ba8b24ee4697ceee159dac4e7"
    assert bundle.protocol_version == 1
    assert bundle.minimum_protocol_version == 1
    assert bundle.minimum_policy_version == 1
    assert bundle.minimum_capability_version == 1
    assert bundle.security_epoch == 1
    assert 3 not in {
        bundle.protocol_version,
        bundle.minimum_protocol_version,
        bundle.minimum_policy_version,
        bundle.minimum_capability_version,
        bundle.security_epoch,
    }


@pytest.mark.parametrize("name", ["capability-snapshot.schema.json", "manifest.json"])
def test_verifier_rejects_any_vendored_byte_drift(tmp_path, name: str) -> None:
    candidate = tmp_path / "protocol"
    shutil.copytree(VENDORED_PROTOCOL_ROOT, candidate)
    path = candidate / name
    path.write_bytes(path.read_bytes() + b"\n")

    with pytest.raises(MemoryProtocolError) as error:
        verify_vendored_protocol_bundle(candidate)

    assert error.value.code == "protocol_bundle_invalid"


def test_verifier_rejects_missing_extra_and_symlinked_bundle_members(tmp_path) -> None:
    missing = tmp_path / "missing"
    shutil.copytree(VENDORED_PROTOCOL_ROOT, missing)
    (missing / "receipt.schema.json").unlink()
    with pytest.raises(MemoryProtocolError, match="missing or unexpected"):
        verify_vendored_protocol_bundle(missing)

    extra = tmp_path / "extra"
    shutil.copytree(VENDORED_PROTOCOL_ROOT, extra)
    (extra / "unexpected.json").write_text("{}", encoding="utf-8")
    with pytest.raises(MemoryProtocolError, match="missing or unexpected"):
        verify_vendored_protocol_bundle(extra)

    linked = tmp_path / "linked"
    shutil.copytree(VENDORED_PROTOCOL_ROOT, linked)
    target = linked / "receipt.schema.json"
    target.unlink()
    target.symlink_to(VENDORED_PROTOCOL_ROOT / "receipt.schema.json")
    with pytest.raises(MemoryProtocolError, match="non-symlink"):
        verify_vendored_protocol_bundle(linked)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable")
def test_verifier_rejects_protocol_fifo_without_blocking(tmp_path) -> None:
    candidate = tmp_path / "protocol"
    shutil.copytree(VENDORED_PROTOCOL_ROOT, candidate)
    path = candidate / "capability-envelope.schema.json"
    path.unlink()
    os.mkfifo(path, 0o600)

    started = time.monotonic()
    with pytest.raises(MemoryProtocolError) as error:
        verify_vendored_protocol_bundle(candidate)
    elapsed = time.monotonic() - started

    assert error.value.code == "protocol_bundle_invalid"
    assert elapsed < 1.0


def test_strict_json_preflights_exact_primitives_before_serialization() -> None:
    with pytest.raises(MemoryProtocolError, match="exact bytes"):
        strict_json_loads(ExplodingMapping(), context="lazy mapping")

    class ExplodingDict(dict):
        def items(self):
            raise AssertionError("dict subclass was evaluated")

    with pytest.raises(MemoryProtocolError, match="exact bytes"):
        strict_json_loads(ExplodingDict(ok=True), context="dict subclass")
    with pytest.raises(MemoryProtocolError, match="non-primitive"):
        strict_json_loads({"lazy": object()}, context="lazy value")

    deep: dict = {}
    cursor = deep
    for _ in range(MAX_JSON_DEPTH + 2):
        child: dict = {}
        cursor["child"] = child
        cursor = child
    with pytest.raises(MemoryProtocolError, match="structure bound"):
        strict_json_loads(deep, context="deep mapping")

    with pytest.raises(MemoryProtocolError, match="node bound"):
        strict_json_loads({"items": [None] * MAX_JSON_NODES}, context="huge list")
    with pytest.raises(MemoryProtocolError, match="text bound"):
        strict_json_loads({"text": "x" * (MAX_JSON_BYTES + 1)}, context="huge text")
    with pytest.raises(MemoryProtocolError, match="text bound"):
        strict_json_loads(
            {"text": "\U0001f642" * ((MAX_JSON_BYTES // 4) + 1)},
            context="huge unicode text",
        )
    with pytest.raises(MemoryProtocolError, match="out-of-bounds integer"):
        strict_json_loads({"integer": 10**10_000}, context="huge integer")
    with pytest.raises(MemoryProtocolError, match="out-of-bounds integer"):
        strict_json_loads(
            '{"integer":' + ("9" * 1_000) + "}",
            context="huge encoded integer",
        )


def test_runtime_binding_authenticates_manifest_policy_config_target_and_bundle() -> None:
    payload = runtime_manifest()
    binding = load(payload)

    assert binding.manifest_digest == canonical_json_digest(payload)
    assert binding.protocol_bundle_digest == EXPECTED_PROTOCOL_BUNDLE_DIGEST
    assert binding.policy_digest == POLICY_DIGEST
    assert binding.config_digest == CONFIG_DIGEST
    assert binding.destination == "hermes-production"
    assert binding.audience == "hermes-agent"
    assert binding.capability_issuer == "cloudseed-memory-control"
    assert binding.protocol_version == 1
    assert binding.supported_protocol_versions == (1,)
    assert binding.minimum_protocol_version == 1
    assert binding.policy_version == 1
    assert binding.supported_policy_versions == (1,)
    assert binding.minimum_policy_version == 1
    assert binding.capability_version == 1
    assert binding.supported_capability_versions == (1,)
    assert binding.minimum_capability_version == 1
    assert binding.security_epoch == 1


@pytest.mark.parametrize(
    ("argument", "value", "code"),
    [
        ("expected_manifest_digest", "f" * 64, "runtime_manifest_digest_mismatch"),
        ("observed_policy_digest", "f" * 64, "policy_digest_mismatch"),
        ("observed_config_digest", "f" * 64, "config_digest_mismatch"),
    ],
)
def test_runtime_binding_fails_closed_on_hash_drift(argument: str, value: str, code: str) -> None:
    payload = runtime_manifest()
    with pytest.raises(MemoryProtocolError) as error:
        load(payload, **{argument: value})
    assert error.value.code == code


def test_runtime_binding_rejects_unknown_security_fields_and_duplicate_json_keys() -> None:
    payload = runtime_manifest()
    payload["protocol"]["secret_override"] = 3
    with pytest.raises(MemoryProtocolError) as error:
        load(payload)
    assert error.value.code == "schema_mismatch"

    raw = json.dumps(runtime_manifest(), separators=(",", ":"))
    raw = raw.replace(
        '"schema_version":"memory-runtime-manifest/v3"',
        '"schema_version":"memory-runtime-manifest/v3","schema_version":"memory-runtime-manifest/v3"',
        1,
    )
    with pytest.raises(MemoryProtocolError) as error:
        load_memory_runtime_binding(
            raw,
            expected_manifest_digest="f" * 64,
            observed_policy_digest=POLICY_DIGEST,
            observed_config_digest=CONFIG_DIGEST,
            now=NOW,
        )
    assert error.value.code == "duplicate_json_key"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("minimum_version", 0),
        ("protocol_version", 999),
        ("minimum_policy_version", 0),
        ("policy_version", 999),
        ("minimum_capability_version", 0),
        ("capability_version", 999),
        ("security_epoch", 0),
    ],
)
def test_runtime_cannot_weaken_any_vendored_floor(field: str, value: int) -> None:
    payload = runtime_manifest(**{field: value})
    if field == "protocol_version":
        payload["protocol"]["supported_versions"] = list(range(1, value + 1))
    elif field == "policy_version":
        payload["protocol"]["supported_policy_versions"] = list(range(1, value + 1))
    elif field == "capability_version":
        payload["protocol"]["supported_capability_versions"] = list(range(1, value + 1))
    with pytest.raises(MemoryProtocolError):
        load(payload)


@pytest.mark.parametrize(
    ("current_field", "supported_field"),
    [
        ("protocol_version", "supported_versions"),
        ("policy_version", "supported_policy_versions"),
        ("capability_version", "supported_capability_versions"),
    ],
)
def test_runtime_future_support_is_rejected_for_every_version_dimension(
    current_field: str,
    supported_field: str,
) -> None:
    payload = runtime_manifest(
        **{
            current_field: 999,
            supported_field: list(range(1, 1000)),
        }
    )
    with pytest.raises(MemoryProtocolError) as error:
        load(payload)
    assert error.value.code == "runtime_protocol_range_mismatch"


def test_runtime_huge_future_endpoint_is_rejected_without_materializing_a_range() -> None:
    payload = runtime_manifest(
        protocol_version=MAX_JSON_INTEGER,
        supported_versions=[1],
    )
    started = time.monotonic()
    with pytest.raises(MemoryProtocolError) as error:
        load(payload)
    elapsed = time.monotonic() - started

    assert error.value.code == "runtime_protocol_range_mismatch"
    assert elapsed < 1.0


def test_runtime_manifest_freshness_is_checked() -> None:
    payload = runtime_manifest()
    with pytest.raises(MemoryProtocolError) as error:
        load(payload, now=NOW + timedelta(minutes=11))
    assert error.value.code == "runtime_expired"
