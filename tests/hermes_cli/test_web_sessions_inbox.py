"""REST-only Inbox scope and backward-compatibility checks."""
import sys

from hermes_state import SessionDB
from tests.tui_gateway.test_methods_mobile import mobile_home
from tests.tui_gateway.test_inbox_summaries import inbox, request, row
from tui_gateway import server, server_requests


def test_unknown_runtime_plane_is_not_authoritative_empty(inbox, monkeypatch):
    monkeypatch.delitem(sys.modules, "tui_gateway.server")
    response = inbox.get("/api/sessions", params={"profile": "ops"})
    assert response.status_code == 200
    scope = response.json()["inbox_summary_scope"]
    assert scope["pending_complete"] is False
    assert "pending_epoch" not in scope
    assert scope["replies_complete"] is True
    assert "attention" not in row(inbox, "ops")


def test_finalized_runtime_cannot_supply_attention(inbox):
    request("ops", "approval")
    server._sessions["ops"]["_finalized"] = True
    assert "attention" not in row(inbox, "ops")


def test_summary_fields_do_not_change_old_session_metadata(inbox):
    baseline = row(inbox, "ops")
    request("ops", "clarify")
    updated = row(inbox, "ops")
    assert updated.pop("attention")["kind"] == "clarify"
    assert updated == baseline
    assert updated["id"] == "same" and updated["profile"] == "ops"
    assert updated["title"] == "Ordinary"
    assert updated["archived"] is False and updated["pinned"] is False


def test_multiple_pending_requests_revision_and_precedence(inbox):
    clarify = request("ops", "clarify")
    first = row(inbox, "ops")["attention"]
    approval = request("ops", "approval")
    second = row(inbox, "ops")["attention"]
    assert second["count"] == 2 and second["kind"] == "approval"
    assert second["revision"] != first["revision"]
    assert server_requests.resolve_response({"id": approval.id, "result": {}})
    assert row(inbox, "ops")["attention"] == first
    assert server_requests.cancel("ops") == 1
    assert "attention" not in row(inbox, "ops")


def test_archived_count_is_filtered_not_an_archived_page_read(inbox, mobile_home):
    home = mobile_home / "profiles" / "ops"
    with SessionDB(db_path=home / "state.db") as db:
        for sid, source in [("archived-desktop", "desktop"), ("archived-cron", "cron")]:
            db.create_session(sid, source)
            db.set_session_archived(sid, True)
    response = inbox.get("/api/sessions", params={"profile": "ops", "source": "desktop", "limit": 0})
    assert response.status_code == 200
    assert response.json()["sessions"] == []
    assert response.json()["archived_count"] == 1
