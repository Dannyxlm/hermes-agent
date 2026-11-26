"""Tests for cross-profile files.index/links.index (all_profiles=True), explicit artifacts, and multi-profile warmer."""
import json
import sqlite3
from pathlib import Path
import pytest

from hermes_state import SessionDB
from plugins.cloudseed_mobile.deliverables import project
from plugins.cloudseed_mobile.dashboard.plugin_api import deliverable_store, warm_indexes
from tui_gateway import server
from tests.tui_gateway.test_methods_mobile import mobile_home, peer, rpc  # noqa: F401


@pytest.fixture
def multi_profile_indexed(mobile_home, peer, tmp_path, monkeypatch):
    root = tmp_path / "ws"
    (root / "outputs").mkdir(parents=True)
    ava_img = root / "outputs" / "ava-shot.png"
    matisse_img = root / "outputs" / "matisse-hero.png"
    iris_report = root / "outputs" / "iris-qa.md"
    ava_img.write_bytes(b"\x89PNG\r\n\x1a\nava")
    matisse_img.write_bytes(b"\x89PNG\r\n\x1a\nmatisse")
    iris_report.write_text("# Iris QA PASS\n")

    # Configure owner & workspace_root on default and sibling profiles (matisse, iris)
    (mobile_home / "profiles" / "matisse").mkdir(parents=True, exist_ok=True)
    (mobile_home / "profiles" / "iris").mkdir(parents=True, exist_ok=True)
    cfg = (
        f"cloudseed_mobile:\n"
        f"  owner_provider: fixture-auth\n"
        f"  owner_user_id: fixture-owner\n"
        f"  workspace_root: {tmp_path}\n"
    )
    (mobile_home / "config.yaml").write_text(cfg)
    for name in ("ops", "matisse", "iris"):
        (mobile_home / "profiles" / name / "config.yaml").write_text(
            f"cloudseed_mobile:\n  workspace_root: {tmp_path}\n"
        )

    # Seed messages in default (t=100), matisse (t=300), iris (t=200)
    with SessionDB(db_path=mobile_home / "state.db") as db:
        db.append_message("root", "assistant", f"MEDIA:{ava_img}\nSee https://example.com/ava")
    with sqlite3.connect(mobile_home / "state.db") as conn:
        conn.execute("UPDATE messages SET timestamp = 100.0")

    matisse_home = mobile_home / "profiles" / "matisse"
    with SessionDB(db_path=matisse_home / "state.db") as db:
        db.create_session("mat-1", "desktop")
        db.set_session_title("mat-1", "Matisse Design")
        db.append_message("mat-1", "assistant", f"MEDIA:{matisse_img}\nhttps://example.com/matisse")
    with sqlite3.connect(matisse_home / "state.db") as conn:
        conn.execute("UPDATE messages SET timestamp = 300.0")

    iris_home = mobile_home / "profiles" / "iris"
    with SessionDB(db_path=iris_home / "state.db") as db:
        db.create_session("iris-1", "desktop")
        db.set_session_title("iris-1", "Iris QA")
        db.append_message(
            "iris-1",
            "tool",
            json.dumps({"artifacts": [{"path": str(iris_report), "title": "Iris QA Report"}]}),
            tool_name="qa_runner",
        )
    with sqlite3.connect(iris_home / "state.db") as conn:
        conn.execute("UPDATE messages SET timestamp = 200.0")

    # Warm each profile's files_index
    for pname, phome in [("default", mobile_home), ("ops", mobile_home / "profiles" / "ops"),
                         ("matisse", matisse_home), ("iris", iris_home)]:
        with server._session_profile_runtime_scope({"profile_home": str(phome)}, hydrate_secrets=False):
            deliverable_store(pname, files_index=True).refresh(max_sessions=50)

    peer.auth_identity = {"provider": "fixture-auth", "user_id": "fixture-owner"}
    return mobile_home, root


def test_files_index_all_profiles_merges_and_sorts(multi_profile_indexed):
    res = rpc("files.index", profile="default", all_profiles=True, limit=10)
    assert "result" in res, res
    page = res["result"]
    items = page["items"]
    names = [(i["profile"], i["display_name"]) for i in items]
    assert names == [
        ("matisse", "matisse-hero.png"),
        ("iris", "iris-qa.md"),
        ("default", "ava-shot.png"),
    ]
    assert page["totals"]["media"] == 2
    assert page["totals"]["files"] == 1
    assert page["coverage"]["complete"] is True
    # Paginate with limit=2
    p1 = rpc("files.index", profile="default", all_profiles=True, limit=2)["result"]
    assert len(p1["items"]) == 2
    assert p1["next_cursor"]
    p2 = rpc("files.index", profile="default", all_profiles=True, limit=2, cursor=p1["next_cursor"])["result"]
    assert [i["display_name"] for i in p2["items"]] == ["ava-shot.png"]
    assert p2["next_cursor"] is None
    # Mismatched query with cursor returns 4091
    stale = rpc("files.index", profile="default", all_profiles=True, limit=2, cursor=p1["next_cursor"], category="media")
    assert stale["error"]["code"] == 4091


def test_links_index_all_profiles_and_guards(multi_profile_indexed, peer):
    res = rpc("links.index", profile="default", all_profiles=True)
    assert "result" in res, res
    urls = [(i["profile"], i["url"]) for i in res["result"]["items"]]
    assert urls == [
        ("matisse", "https://example.com/matisse"),
        ("default", "https://example.com/ava"),
    ]
    # Rejects session_id / workspace_id when all_profiles=True
    assert rpc("files.index", profile="default", all_profiles=True, session_id="root")["error"]["code"] == 4400
    assert rpc("files.index", profile="default", all_profiles=True, workspace_id="w1")["error"]["code"] == 4400
    # Owner check still enforced
    peer.auth_identity = {"provider": "foreign", "user_id": "intruder"}
    assert rpc("files.index", profile="default", all_profiles=True)["error"]["code"] == 4403


def test_warm_indexes_includes_sibling_profiles(multi_profile_indexed):
    indexes = warm_indexes()
    profiles = {idx.profile for idx in indexes}
    assert {"default", "matisse", "iris", "ops"}.issubset(profiles)


def test_explicit_artifacts_payload_and_extended_producers(tmp_path):
    ws = [{"id": "w1", "name": "ws", "root_path": str(tmp_path)}]
    img = tmp_path / "shot.png"
    doc = tmp_path / "summary.md"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    doc.write_text("# hi")
    msgs = [
        {"id": 1, "role": "tool", "tool_name": "take_screenshot", "timestamp": 10.0,
         "content": json.dumps({"screenshot_path": str(img)})},
        {"id": 2, "role": "tool", "tool_name": "custom_runner", "timestamp": 20.0,
         "content": json.dumps({"artifacts": [str(doc)]})},
    ]
    rows = project("matisse", {"id": "s1", "title": "T"}, msgs, ws, index_evidence=True)
    by_name = {r["display_name"]: r for r in rows}
    assert by_name["shot.png"]["action"] == "created"
    assert by_name["summary.md"]["action"] == "created"
    assert by_name["summary.md"]["provenance"] == "tool_artifact"
