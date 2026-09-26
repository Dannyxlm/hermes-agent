"""Managed upstream counts remain readable by Dashboard and remote Desktop."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import hermes_cli.web_server as server


@pytest.fixture
def managed_status(tmp_path, monkeypatch):
    root = tmp_path / "release"
    root.mkdir()
    status_path = tmp_path / "upstream-status.json"
    monkeypatch.setattr(server, "PROJECT_ROOT", root)
    monkeypatch.setattr(server, "_MANAGED_UPDATE_STATUS_PATH", status_path)
    monkeypatch.setenv("HERMES_DASHBOARD_UPDATE_MANAGED_EXTERNALLY", "1")
    monkeypatch.setattr(
        server, "get_version_info", lambda: SimpleNamespace(derived_version="1.2.3+gabcdef12")
    )
    now = datetime.now(timezone.utc)
    receipt = {
        "schema_version": "hermes-update-status.v2",
        "count_basis": "recorded_official_base",
        "running_release": "managed-test-release",
        "release_id": "managed-test-release",
        "hermes_version": "1.2.3",
        "running_source": "c" * 40,
        "running_upstream_base": "a" * 40,
        "tracked_upstream": "NousResearch/main",
        "upstream_head": "b" * 40,
        "running_source_is_ancestor_of_upstream": False,
        "commits_behind": 7,
        "local_patch_count": 1,
        "overlay_count": 1,
        "overlay_ids": ["test-overlay"],
        "carried_commit_count": 3,
        "install_mode": "managed-immutable",
        "last_fetched_at": now.isoformat(),
        "generated_at": now.isoformat(),
        "candidate_status": "current",
        "blockers": [],
        "next_action": "Review upstream changes.",
        "source_worktree_clean": True,
        "source_refs_remotely_reachable": True,
    }
    client = TestClient(server.app)
    client.headers[server._SESSION_HEADER_NAME] = server._SESSION_TOKEN
    return client, status_path, receipt


@pytest.mark.parametrize("behind,stale", [(7, False), (0, False), (7, True)])
def test_managed_route_returns_version_and_monitor_count(managed_status, behind, stale):
    client, status_path, receipt = managed_status
    receipt["commits_behind"] = behind
    if stale:
        age = server._managed_update_max_age_seconds() + 60
        receipt["last_fetched_at"] = receipt["generated_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=age)
        ).isoformat()
    status_path.write_text(json.dumps(receipt))

    response = client.get("/api/hermes/update/check")

    assert response.status_code == 200
    payload = response.json()
    assert payload["current_version"] == server.get_version_info().derived_version
    assert payload["install_method"] == "managed-runtime"
    assert payload["can_apply"] is False
    assert payload["behind"] == behind
    assert payload["update_available"] is (behind > 0)
    assert payload["managed_source"]["commits_behind"] == behind
    assert payload["managed_source"]["availability"] == ("stale" if stale else "ready")
    assert payload["managed_source"]["running_source"] == receipt["running_source"]


@pytest.mark.parametrize("invalid", [False, True])
def test_missing_or_invalid_receipt_is_visible_without_inventing_zero(managed_status, invalid):
    client, status_path, _receipt = managed_status
    if invalid:
        status_path.write_text("not JSON")

    response = client.get("/api/hermes/update/check")

    assert response.status_code == 200
    payload = response.json()
    assert payload["behind"] is None
    assert payload["update_available"] is False
    assert payload["can_apply"] is False
    assert payload["managed_source"]["availability"] == ("invalid" if invalid else "missing")
