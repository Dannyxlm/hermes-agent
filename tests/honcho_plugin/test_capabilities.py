"""Fail-closed, offline tests for the Honcho capability receipt probe."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

import pytest

from plugins.memory.honcho.capabilities import (
    CapabilityCheck,
    HonchoCapabilityProbeDependencies,
    REQUIRED_CAPABILITIES,
    build_public_capability_receipt,
    load_public_capability_receipt,
    probe_honcho_capabilities,
    probe_required_honcho_capabilities,
    validate_public_capability_receipt,
)


SDK_VERSION = "2.2.0"
SDK_SOURCE_HASH = "sha256:sdk-source"
PROVIDER_SOURCE_HASH = "sha256:provider-source"
MANIFEST_HASH = "sha256:probe-manifest"


def _dependencies(*, callables: dict[str, object], readbacks: dict[str, object] | None = None):
    return HonchoCapabilityProbeDependencies(
        sdk_version=SDK_VERSION,
        sdk_source_hash=SDK_SOURCE_HASH,
        provider_source_hash=PROVIDER_SOURCE_HASH,
        probe_manifest_hash=MANIFEST_HASH,
        callables=callables,
        readbacks=readbacks or {},
    )


def _check(name: str, *, readback_shape: object = "mapping", **extra: object) -> CapabilityCheck:
    return CapabilityCheck(
        name=name,
        callable_name=name,
        readback_name=name,
        expected_shape=readback_shape,
        **extra,
    )


def test_supported_receipt_is_bound_and_content_free():
    calls: list[str] = []

    def add_peers():
        calls.append("add_peers")
        return {"status": "accepted"}

    def read_peer_configuration():
        calls.append("read_peer_configuration")
        return {"observe_me": True, "observe_others": False}

    receipt = probe_honcho_capabilities(
        dependencies=_dependencies(
            callables={"add_peers": add_peers},
            readbacks={"add_peers": read_peer_configuration},
        ),
        checks=[_check("add_peers", readback_shape={"observe_me": bool, "observe_others": bool})],
    )

    assert receipt == {
        "schema": "honcho-capability-receipt-v1",
        "state": "supported",
        "sdk_version": SDK_VERSION,
        "sdk_source_hash": SDK_SOURCE_HASH,
        "provider_source_hash": PROVIDER_SOURCE_HASH,
        "probe_manifest_hash": MANIFEST_HASH,
        "checks": {"add_peers": {"state": "passed", "readback": "effective"}},
    }
    assert calls == ["add_peers", "read_peer_configuration"]
    assert "content" not in repr(receipt)


@pytest.mark.parametrize(
    "status",
    ["rejected", "ignored", "missing"],
)
def test_non_accepted_probe_result_is_unsupported_and_does_not_fail_open(status):
    receipt = probe_honcho_capabilities(
        dependencies=_dependencies(callables={"peer": lambda: {"status": status}}),
        checks=[_check("peer", readback_shape={"id": str})],
    )

    assert receipt["state"] == "unsupported_api"
    assert receipt["checks"] == {"peer": {"state": "unsupported_api", "reason": status}}


def test_shape_drift_is_unsupported_without_leaking_readback_content():
    receipt = probe_honcho_capabilities(
        dependencies=_dependencies(
            callables={"peer": lambda: {"status": "accepted"}},
            readbacks={"peer": lambda: {"unexpected": "secret-card-text"}},
        ),
        checks=[_check("peer", readback_shape={"id": str})],
    )

    assert receipt["state"] == "unsupported_api"
    assert receipt["checks"] == {"peer": {"state": "unsupported_api", "reason": "shape_drift"}}
    assert "secret-card-text" not in repr(receipt)


def test_sdk_version_mismatch_is_unsupported_before_probe_call():
    called = False

    def should_not_run():
        nonlocal called
        called = True
        return {"status": "accepted"}

    receipt = probe_honcho_capabilities(
        dependencies=_dependencies(callables={"peer": should_not_run}),
        expected_sdk_version="2.1.0",
        checks=[_check("peer", readback_shape={"id": str})],
    )

    assert receipt["state"] == "unsupported_api"
    assert receipt["checks"] == {"peer": {"state": "unsupported_api", "reason": "version_mismatch"}}
    assert called is False


def test_missing_callable_is_unsupported():
    receipt = probe_honcho_capabilities(
        dependencies=_dependencies(callables={}),
        checks=[_check("peer", readback_shape={"id": str})],
    )

    assert receipt["state"] == "unsupported_api"
    assert receipt["checks"] == {"peer": {"state": "unsupported_api", "reason": "missing"}}


def test_paid_chat_is_not_called_without_an_explicit_approved_manifest():
    called = False

    def paid_chat():
        nonlocal called
        called = True
        raise AssertionError("paid chat must not be probed")

    receipt = probe_honcho_capabilities(
        dependencies=_dependencies(callables={"chat": paid_chat}),
        checks=[_check("chat", paid=True, readback_shape={"answer": str})],
    )

    assert receipt["state"] == "unsupported_api"
    assert receipt["checks"] == {"chat": {"state": "unsupported_api", "reason": "paid_chat_not_approved"}}
    assert called is False


def test_approved_paid_chat_manifest_allows_the_explicit_probe():
    receipt = probe_honcho_capabilities(
        dependencies=_dependencies(
            callables={"chat": lambda: {"status": "accepted"}},
            readbacks={"chat": lambda: {"answer": "redacted-test-fixture"}},
        ),
        approved_test_manifest={"paid_checks": ["chat"]},
        checks=[_check("chat", paid=True, readback_shape={"answer": str})],
    )

    assert receipt["state"] == "supported"
    assert receipt["checks"] == {"chat": {"state": "passed", "readback": "effective"}}
    assert "redacted-test-fixture" not in repr(receipt)


def test_required_probe_rejects_incomplete_manifest_before_any_call():
    called = False

    def operation():
        nonlocal called
        called = True
        return {"status": "accepted", "readback": {"id": "x"}}

    receipt = probe_required_honcho_capabilities(
        _dependencies(callables={"peer_handle": operation}),
        checks=[_check("peer_handle", readback_shape={"id": str})],
    )
    assert receipt["state"] == "unsupported_api"
    assert called is False
    assert set(receipt["checks"]) == REQUIRED_CAPABILITIES - {"peer_handle"}


def _complete_probe_receipt():
    return {
        "schema": "honcho-capability-receipt-v1",
        "state": "supported",
        "sdk_version": SDK_VERSION,
        "sdk_source_hash": "a" * 64,
        "provider_source_hash": "b" * 64,
        "probe_manifest_hash": "c" * 64,
        "checks": {
            name: {"state": "passed", "readback": "effective"}
            for name in REQUIRED_CAPABILITIES
        },
    }


def test_public_receipt_matches_cloudseed_contract_and_loads_by_exact_bytes(tmp_path):
    receipt = build_public_capability_receipt(
        _complete_probe_receipt(),
        config_revision_sha256="d" * 64,
        disposable_workspace_hmac="e" * 64,
        generated_at="2026-07-18T20:00:00Z",
    )
    assert receipt["schema_version"] == "honcho-capability-receipt-v1"
    assert receipt["state"] == "supported"
    assert receipt["paid_chat_probe"] == "approved_minimal_passed"
    assert validate_public_capability_receipt(
        receipt,
        expected_config_revision_sha256="d" * 64,
    )
    payload = json.dumps(receipt, sort_keys=True).encode()
    path = tmp_path / "receipt.json"
    path.write_bytes(payload)
    loaded = load_public_capability_receipt(
        path,
        expected_file_sha256=hashlib.sha256(payload).hexdigest(),
        expected_config_revision_sha256="d" * 64,
    )
    assert loaded == receipt

    assert load_public_capability_receipt(
        path,
        expected_file_sha256="0" * 64,
        expected_config_revision_sha256="d" * 64,
    ) is None
    link = tmp_path / "receipt-link.json"
    link.symlink_to(path)
    assert load_public_capability_receipt(
        link,
        expected_file_sha256=hashlib.sha256(payload).hexdigest(),
    ) is None


def test_public_supported_receipt_rejects_missing_check_or_raw_unknown_field():
    probe = _complete_probe_receipt()
    probe["checks"].pop("human_message_search")
    receipt = build_public_capability_receipt(
        probe,
        config_revision_sha256="d" * 64,
        disposable_workspace_hmac="e" * 64,
        generated_at="2026-07-18T20:00:00Z",
    )
    assert receipt["state"] == "unsupported_api"
    forged = dict(receipt)
    forged["state"] = "supported"
    assert not validate_public_capability_receipt(forged)
    forged = dict(receipt)
    forged["raw_payload"] = "private"
    assert not validate_public_capability_receipt(forged)
