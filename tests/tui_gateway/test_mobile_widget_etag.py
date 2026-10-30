"""The widget Inbox page carries an ETag and answers a matching ``If-None-Match`` with 304.

Additive: a client that never sends ``If-None-Match`` (every shipped build) gets the same 200 body.
"""
import uuid

from fastapi.testclient import TestClient

from hermes_state import SessionDB
from tests.tui_gateway.test_methods_mobile import mobile_home, peer, rpc  # noqa: F401  (fixtures)
from tests.tui_gateway.test_methods_mobile_push import push_service  # noqa: F401  (fixture)

ROUTE = "/api/mobile/widgets/snapshot"


def _client(peer, monkeypatch):  # noqa: F811
    from hermes_cli import web_server
    peer.auth_identity = {"user_id": "fixture-owner", "provider": "fixture-auth"}
    token = "12" * 32
    result = rpc("mobile.widget.inbox.register", profile="ops", installation_id=str(uuid.uuid4()),
                 connection_id=str(uuid.uuid4()), read_token=token)
    assert "result" in result, result
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    return TestClient(web_server.app), {"Authorization": "Bearer " + token}


def test_unchanged_inbox_page_answers_304_and_a_change_answers_200(push_service, mobile_home, peer, monkeypatch):  # noqa: F811
    monkeypatch.setattr("pathlib.Path.home", lambda: mobile_home.parent)
    path = mobile_home / "profiles" / "ops" / "state.db"
    with SessionDB(db_path=path) as db:
        db.create_session("chat", "desktop")
        db.append_message("chat", "assistant", "hello", finish_reason="stop")
    client, headers = _client(peer, monkeypatch)

    first = client.get(ROUTE + "?inbox=1", headers=headers)
    assert first.status_code == 200
    tag = first.headers["etag"]

    again = client.get(ROUTE + "?inbox=1", headers={**headers, "If-None-Match": tag})
    assert again.status_code == 304 and again.content == b""
    assert again.headers["etag"] == tag and again.headers["cache-control"] == "no-store"
    assert client.get(ROUTE + "?inbox=1", headers={**headers, "If-None-Match": '"other", ' + tag}).status_code == 304
    # No validator, or the wrong one: today's full answer.
    assert client.get(ROUTE + "?inbox=1", headers=headers).json()["items"] == first.json()["items"]
    assert client.get(ROUTE + "?inbox=1", headers={**headers, "If-None-Match": '"stale"'}).status_code == 200

    with SessionDB(db_path=path) as db:
        db.append_message("chat", "assistant", "a newer reply", finish_reason="stop")
    changed = client.get(ROUTE + "?inbox=1", headers={**headers, "If-None-Match": tag})
    assert changed.status_code == 200 and changed.headers["etag"] != tag
    assert changed.json()["revision"] != first.json()["revision"]


def test_each_page_has_its_own_tag(push_service, mobile_home, peer, monkeypatch):  # noqa: F811
    monkeypatch.setattr("pathlib.Path.home", lambda: mobile_home.parent)
    path = mobile_home / "profiles" / "ops" / "state.db"
    with SessionDB(db_path=path) as db:
        for n in range(105):
            db.create_session(f"chat-{n}", "desktop")
            db.append_message(f"chat-{n}", "assistant", f"fixture {n}", finish_reason="stop")
    client, headers = _client(peer, monkeypatch)
    first = client.get(ROUTE + "?inbox=1", headers=headers)
    cursor = first.json()["next_cursor"]
    second = client.get(ROUTE, params={"inbox": "1", "cursor": cursor}, headers=headers)
    assert second.status_code == 200 and second.headers["etag"] != first.headers["etag"]
    # A page's tag never validates a different page.
    assert client.get(ROUTE, params={"inbox": "1", "cursor": cursor},
                      headers={**headers, "If-None-Match": first.headers["etag"]}).status_code == 200
