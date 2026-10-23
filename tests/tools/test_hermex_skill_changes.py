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


def test_staged_write_never_becomes_learned_card(change_home, monkeypatch):
    from tools import skill_manager_tool as manager
    monkeypatch.setattr(manager, "_run_write_gate", lambda *a, **k: json.dumps({"success": True, "staged": True}))
    assert mutate(action="create", content="---\nname: proof\ndescription: Proof.\n---\nBefore.\n")["staged"]
    assert result("skills.changes.list", session_id="chat")["items"] == []
    assert not (change_home / "skills/proof/SKILL.md").exists()


def test_create_undo_preserves_unrelated_later_file(change_home):
    assert mutate(action="create", content="---\nname: proof\ndescription: Proof.\n---\nBefore.\n")["success"]
    item = result("skills.changes.list", session_id="chat")["items"][0]
    unrelated = change_home / "skills/proof/references/later.md"
    unrelated.parent.mkdir()
    unrelated.write_text("Unrelated later content.")
    decide(item)
    assert not (change_home / "skills/proof/SKILL.md").exists()
    assert unrelated.read_text() == "Unrelated later content."


def test_symlink_target_and_stale_revision_refused(change_home):
    item = make_change()
    path = change_home / "skills/proof/SKILL.md"
    outside = change_home.parent / "outside.md"
    outside.write_text("After.")
    path.unlink()
    path.symlink_to(outside)
    assert "error" in rpc("skills.changes.decide", change_id=item["change_id"], decision="undo",
                           expected_revision=item["revision"], client_action_id="symlink")
    assert outside.read_text() == "After."
    assert "error" in rpc("skills.changes.decide", change_id=item["change_id"], decision="keep",
                           expected_revision="stale", client_action_id="stale")


def test_uncertain_undo_recovers_on_get_without_replaying(change_home, monkeypatch):
    import hermex_skill_changes as changes
    item = make_change()
    original = changes._durable_bytes
    def disk_error(path, data):
        if path.name == "SKILL.md":
            raise OSError("fixture write interruption")
        return original(path, data)
    monkeypatch.setattr(changes, "_durable_bytes", disk_error)
    failed = rpc("skills.changes.decide", change_id=item["change_id"], decision="undo",
                 expected_revision=item["revision"], client_action_id="uncertain")
    assert failed["error"]["code"] == 4401
    assert "After." in (change_home / "skills/proof/SKILL.md").read_text()
    monkeypatch.setattr(changes, "_durable_bytes", original)
    readback = result("skills.changes.get", change_id=item["change_id"])
    assert readback["decision"] == "undone" and readback["receipt_id"]
    assert "Before." in (change_home / "skills/proof/SKILL.md").read_text()


def test_partial_undo_readback_does_not_overwrite_newer_edit(change_home, monkeypatch):
    import hermex_skill_changes as changes
    from tools.skill_manager_tool import skill_manage
    assert mutate(action="create", content="---\nname: proof\ndescription: Proof.\n---\nBefore.\n")["success"]
    done = json.loads(skill_manage(action="", name="", session_id="chat", operations=[
        {"action": "patch", "name": "proof", "old_string": "Before.", "new_string": "After."},
        {"action": "write_file", "name": "proof", "file_path": "references/new.md", "file_content": "Created."},
    ]))
    assert done["success"]
    item = result("skills.changes.list", session_id="chat")["items"][0]
    original = changes._save_journal
    def lose_receipt(journal):
        if any(a["status"] == "complete" for a in journal["actions"].values()):
            raise OSError("fixture lost final receipt write")
        original(journal)
    monkeypatch.setattr(changes, "_save_journal", lose_receipt)
    assert "error" in rpc("skills.changes.decide", change_id=item["change_id"], decision="undo",
                           expected_revision=item["revision"], client_action_id="partial")
    assert "Before." in (change_home / "skills/proof/SKILL.md").read_text()
    assert mutate(action="patch", old_string="Before.", new_string="Newer.")["success"]
    monkeypatch.setattr(changes, "_save_journal", original)
    assert rpc("skills.changes.get", change_id=item["change_id"])["error"]["code"] == 4091
    assert "Newer." in (change_home / "skills/proof/SKILL.md").read_text()


def test_incomplete_before_capture_cannot_delete_an_existing_file(change_home, monkeypatch):
    from tools import skill_ledger
    assert mutate(action="create", content="---\nname: proof\ndescription: Proof.\n---\nBefore.\n")["success"]
    monkeypatch.setattr(skill_ledger, "capture_before", lambda *a, **k: [])
    assert mutate(action="patch", old_string="Before.", new_string="After.")["success"]
    item = result("skills.changes.list", session_id="chat")["items"][0]
    assert item["can_undo"] is False
    assert "error" in rpc("skills.changes.decide", change_id=item["change_id"], decision="undo",
                           expected_revision=item["revision"], client_action_id="incomplete")
    assert "After." in (change_home / "skills/proof/SKILL.md").read_text()


def test_alias_writers_share_the_skill_lock(change_home):
    from tools.skill_manager_tool import _skill_lock_path, skill_manage
    assert mutate(action="create", content="---\nname: alternate-name\ndescription: Proof.\n---\nBefore.\n")["success"]
    assert _skill_lock_path("alternate-name") == _skill_lock_path("proof")
    assert json.loads(skill_manage(action="patch", name="alternate-name", session_id="chat",
                                  old_string="Before.", new_string="After."))["success"]
    item = result("skills.changes.list", session_id="chat")["items"][0]
    decide(item)
    assert "Before." in (change_home / "skills/proof/SKILL.md").read_text()


@pytest.mark.parametrize("created", [False, True])
def test_undo_records_one_audit_row_and_usage(change_home, created):
    from tools import skill_ledger, skill_usage
    if created:
        assert mutate(action="create", content="---\nname: proof\ndescription: Proof.\n---\nBefore.\n")["success"]
        item = result("skills.changes.list", session_id="chat")["items"][0]
    else:
        item = make_change()
    before = skill_usage.load_usage().get("proof", {}).get("patch_count", 0)
    receipt = decide(item)
    assert decide(item) == receipt
    result("skills.changes.get", change_id=item["change_id"])
    rows = [e for e in skill_ledger.list_entries() if e["action"] == "undo"]
    assert len(rows) == 1
    row = rows[0]
    assert row["evidence"]["undo_of"] == item["change_id"]
    assert row["evidence"]["session_id"] == "chat"
    assert row["evidence"]["review_committed"] is False
    assert row["before"]
    usage = skill_usage.load_usage()
    if created:
        assert "proof" not in usage
    else:
        assert usage["proof"]["patch_count"] == before + 1


@pytest.mark.parametrize("batch", [False, True])
def test_review_import_failure_preserves_mutation_before_capture(change_home, monkeypatch, batch):
    import hashlib
    import sys
    from tools import skill_ledger
    from tools.skill_manager_tool import skill_manage
    assert mutate(action="create", content="---\nname: proof\ndescription: Proof.\n---\nBefore.\n")["success"]
    original = (change_home / "skills/proof/SKILL.md").read_bytes()
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "hermex_skill_changes", None)
        if batch:
            response = json.loads(skill_manage(action="", name="", session_id="chat", operations=[
                {"name":"proof", "action":"patch", "old_string":"Before.", "new_string":"After."}]))
        else:
            response = mutate(action="patch", old_string="Before.", new_string="After.")
    assert response["success"]
    row = skill_ledger.list_entries()[0]
    assert row["before"]
    assert row["before"][0]["sha256"] == hashlib.sha256(original).hexdigest()


def test_lost_undo_acknowledgement_does_not_duplicate_audit_or_usage(change_home, monkeypatch):
    import hermex_skill_changes as changes
    from tools import skill_ledger, skill_usage
    item = make_change()
    original = changes._save_journal
    def lose_completion(journal):
        if any(a["status"] == "complete" for a in journal["actions"].values()):
            raise OSError("lost acknowledgement")
        return original(journal)
    monkeypatch.setattr(changes, "_save_journal", lose_completion)
    failed = rpc("skills.changes.decide", change_id=item["change_id"], decision="undo",
                 expected_revision=item["revision"], client_action_id="lost-ack")
    assert failed["error"]["code"] == 4401
    assert len([e for e in skill_ledger.list_entries() if e["action"] == "undo"]) == 1
    patched = skill_usage.load_usage()["proof"]["patch_count"]
    monkeypatch.setattr(changes, "_save_journal", original)
    recovered = result("skills.changes.get", change_id=item["change_id"])
    assert recovered["decision"] == "undone"
    assert len([e for e in skill_ledger.list_entries() if e["action"] == "undo"]) == 1
    assert skill_usage.load_usage()["proof"]["patch_count"] == patched
