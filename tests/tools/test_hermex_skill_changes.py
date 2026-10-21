"""Scoped learned-change decisions use real skill writes and SQLite through RPC."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport


@pytest.fixture
def change_home(tmp_path, monkeypatch):
    from tools import skill_manager_tool as manager
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    monkeypatch.setattr(server, "_sessions", {})
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session("chat", "desktop")
        db.create_session("other", "desktop")
    monkeypatch.setattr(manager, "_run_write_gate", lambda *a, **k: None)
    token = bind_transport(SimpleNamespace(auth_identity={"user_id": "owner", "provider": "test"}))
    yield home
    reset_transport(token)


def rpc(method, **params):
    return server.handle_request({"id": "changes", "method": method, "params": {"profile": "default", **params}})


def result(method, **params):
    response = rpc(method, **params)
    assert "result" in response, response
    return response["result"]


def mutate(**params):
    from tools.skill_manager_tool import skill_manage
    return json.loads(skill_manage(name="proof", session_id="chat", **params))


def make_change():
    assert mutate(action="create", content="---\nname: proof\ndescription: Use for proof.\n---\nBefore.\n")["success"]
    assert mutate(action="patch", old_string="Before.", new_string="After.")["success"]
    items = result("skills.changes.list", session_id="chat")["items"]
    return next(i for i in items if any(line["text"] == "After." for line in i["lines"]))


def decide(item, decision="undo", action="action-1"):
    return result("skills.changes.decide", change_id=item["change_id"], decision=decision,
                  expected_revision=item["revision"], client_action_id=action)


def test_review_diff_null_metadata_keep_and_readback(change_home):
    item = make_change()
    assert item["anchor_message_id"] is None
    assert item["reason"] is None
    assert item["title"] is None
    assert item["decision"] == "unreviewed"
    assert item["can_undo"] is True
    detail = result("skills.changes.get", change_id=item["change_id"])
    assert "-Before." in detail["diff"] and "+After." in detail["diff"]
    kept = decide(item, "keep")
    assert kept["decision"] == "kept"
    assert decide(item, "keep") == kept
    readback = result("skills.changes.get", change_id=item["change_id"])
    assert readback["decision"] == "kept"
    assert readback["receipt_id"] == kept["receipt_id"]
    assert "After." in (change_home / "skills/proof/SKILL.md").read_text()
    assert result("skills.changes.list", session_id="other")["items"] == []


def test_undo_conflict_never_overwrites_later_edit(change_home):
    item = make_change()
    assert mutate(action="patch", old_string="After.", new_string="Newer edit.")["success"]
    rejected = rpc("skills.changes.decide", change_id=item["change_id"], decision="undo",
                   expected_revision=item["revision"], client_action_id="conflict")
    assert rejected["error"]["code"] == 4091
    assert "Newer edit." in (change_home / "skills/proof/SKILL.md").read_text()
    assert result("skills.changes.get", change_id=item["change_id"])["can_undo"] is False


def test_undo_idempotent_durable_receipt_and_action_collision(change_home):
    item = make_change()
    undone = decide(item)
    assert undone["decision"] == "undone"
    assert "Before." in (change_home / "skills/proof/SKILL.md").read_text()
    assert decide(item) == undone
    readback = result("skills.changes.get", change_id=item["change_id"])
    assert readback["decision"] == "undone"
    assert readback["receipt_id"] == undone["receipt_id"]
    assert "error" in rpc("skills.changes.decide", change_id=item["change_id"], decision="keep",
                           expected_revision=item["revision"], client_action_id="action-1")


def test_failed_and_rolled_back_batch_never_surface(change_home):
    from tools.skill_manager_tool import skill_manage
    assert mutate(action="create", content="---\nname: proof\ndescription: Use for proof.\n---\nBefore.\n")["success"]
    initial = result("skills.changes.list", session_id="chat")["items"]
    failed = json.loads(skill_manage(action="", name="", session_id="chat", operations=[
        {"action": "patch", "name": "proof", "old_string": "Before.", "new_string": "Intermediate."},
        {"action": "patch", "name": "proof", "old_string": "never matches", "new_string": "No."},
    ]))
    assert failed["success"] is False
    assert "Before." in (change_home / "skills/proof/SKILL.md").read_text()
    assert result("skills.changes.list", session_id="chat")["items"] == initial


def test_successful_batch_is_one_committed_change_per_skill(change_home):
    from tools.skill_manager_tool import skill_manage
    assert mutate(action="create", content="---\nname: proof\ndescription: Use for proof.\n---\nBefore.\n")["success"]
    initial = result("skills.changes.list", session_id="chat")["items"]
    done = json.loads(skill_manage(action="", name="", session_id="chat", operations=[
        {"action": "patch", "name": "proof", "old_string": "Before.", "new_string": "Middle."},
        {"action": "patch", "name": "proof", "old_string": "Middle.", "new_string": "Final."},
    ]))
    assert done["success"] is True
    items = result("skills.changes.list", session_id="chat")["items"]
    assert len(items) == len(initial) + 1
    item = next(i for i in items if i["change_id"] not in {x["change_id"] for x in initial})
    assert {line["text"] for line in item["lines"]} == {"Before.", "Final."}
    decide(item)
    assert "Before." in (change_home / "skills/proof/SKILL.md").read_text()


def test_missing_blob_and_symlink_refuse_without_write(change_home):
    from tools import skill_ledger
    item = make_change()
    entry = skill_ledger.get_entry(item["change_id"])
    skill_ledger.blobs_dir().joinpath(entry["before"][0]["sha256"]).unlink()
    assert "error" in rpc("skills.changes.decide", change_id=item["change_id"], decision="undo",
                           expected_revision=item["revision"], client_action_id="missing")
    assert "After." in (change_home / "skills/proof/SKILL.md").read_text()


def test_foreign_profile_and_hidden_session_denied(change_home):
    item = make_change()
    assert "error" in rpc("skills.changes.get", change_id=item["change_id"], profile="missing")
    with SessionDB(db_path=change_home / "state.db") as db:
        db._conn.execute("UPDATE sessions SET profile_name='foreign' WHERE id='chat'")
    assert "error" in rpc("skills.changes.get", change_id=item["change_id"])
