"""Reply-specific read state agrees across REST and the live native roster."""

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_inbox_summaries import inbox, row
from tests.tui_gateway.test_methods_mobile import mobile_home, peer, rpc
from tui_gateway import server


@pytest.mark.parametrize("watermark,expected", [(None, False), (-1, False), (0, True), (99, True),
                                                 (100, False), (101, False)])
def test_reply_unread_uses_root_watermark_not_later_activity(inbox, mobile_home, peer,
                                                           watermark, expected):
    home = mobile_home / "profiles" / "ops"
    with SessionDB(db_path=home / "state.db") as db:
        reply_id = db.append_message("same", "assistant", "root final", timestamp=100,
                                     finish_reason="stop")
        db._conn.execute("UPDATE sessions SET end_reason='compression', ended_at=101, "
                         "last_read_at=? WHERE id='same'", (watermark,))
        db.create_session("tip", "desktop", parent_session_id="same")
        db.append_message("tip", "assistant", "tool preamble", timestamp=102,
                          finish_reason="tool_calls")
        db.append_message("tip", "user", "later activity", timestamp=103)
        db.create_session("branch", "desktop", parent_session_id="same",
                          model_config={"_branched_from": "same"})
        db.append_message("branch", "assistant", "branch final", timestamp=104)
    server._sessions["ops"]["session_key"] = "tip"
    response = inbox.get("/api/sessions", params={"profile": "ops"}).json()
    reply = next(r for r in response["sessions"] if r["id"] == "tip")["last_assistant_reply"]
    assert reply == {"row_id": reply_id, "at": 100, "preview": "root final", "unread": expected}
    branch = next(r for r in response["sessions"] if r["id"] == "branch")
    assert branch["last_assistant_reply"]["unread"] is False
    live = rpc("session.active_list", profile="ops")["result"]["sessions"][0]
    assert live["last_assistant_reply"] == reply
    assert "last_assistant_reply" not in row(inbox, "default")


def test_inbox_v2_capability(mobile_home, peer):
    capabilities = rpc("mobile.capabilities")["result"]
    assert capabilities["feature_versions"]["inbox_summaries"] == 2