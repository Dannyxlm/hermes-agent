"""An empty ``changed_since`` list read is answered from the change journal, without opening the store.

Counted by the stores the route opens (``_open_session_db_at_path``), not by timing. The phone asks
this question every few seconds of foreground; nearly every answer is "nothing".
"""
import pytest

from hermes_cli import web_server_sessions
from hermes_cli.web_routers import sessions
from hermes_state import SessionDB
from tests.tui_gateway.test_inbox_summaries import inbox  # fixture
from tests.tui_gateway.test_methods_mobile import mobile_home  # fixture
from tests.tui_gateway.test_session_change_cursor import PHONE_EXCLUDES
from tui_gateway import server, session_change_cursor


@pytest.fixture(autouse=True)
def clean_journal(monkeypatch):
    session_change_cursor.reset_for_tests()
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    monkeypatch.setattr(sessions, "_LIST_TOTALS", {}, raising=False)
    yield
    session_change_cursor.reset_for_tests()


@pytest.fixture
def opened(monkeypatch):
    stores = []
    real = web_server_sessions._open_session_db_at_path

    def spy(path, **kwargs):
        stores.append(path)
        return real(path, **kwargs)

    monkeypatch.setattr(web_server_sessions, "_open_session_db_at_path", spy)
    return stores


def _list(client, **params):
    response = client.get("/api/sessions", params={
        "profile": "ops", "order": "recent", "exclude_sources": PHONE_EXCLUDES, **params})
    assert response.status_code == 200, response.text
    return response.json()


def _write(mobile_home, fn):
    with SessionDB(db_path=mobile_home / "profiles" / "ops" / "state.db") as db:
        fn(db)


def test_nothing_changed_opens_no_store_and_answers_like_a_read(inbox, mobile_home, opened):
    full = _list(inbox, change_cursor=1)
    cursor = full["change_cursor"]
    opened.clear()

    answers = [_list(inbox, changed_since=cursor) for _ in range(3)]

    assert opened == []
    for answer in answers:
        assert answer["sessions"] == [] and answer["tombstones"] == []
        assert answer["delta"] is True and answer["repair"] is False and answer["change_cursor"] == cursor
        assert (answer["total"], answer["archived_count"]) == (full["total"], full["archived_count"])
        assert answer["storage"] == full["storage"] and answer["limit"] == full["limit"] and answer["offset"] == 0
        assert answer["inbox_summary_scope"] == full["inbox_summary_scope"]


def test_totals_follow_every_change_and_never_outlive_it(inbox, mobile_home):
    cursor = _list(inbox, change_cursor=1)["change_cursor"]
    for change in (
        lambda db: (db.create_session("fresh", "desktop"), db.set_session_title("fresh", "Fresh")),
        lambda db: db.set_session_archived("same", True),
        lambda db: db.delete_session("fresh"),
    ):
        _write(mobile_home, change)
        moved = _list(inbox, changed_since=cursor)
        assert moved["delta"] is True and moved["repair"] is False and moved["change_cursor"] != cursor
        cursor = moved["change_cursor"]
        quiet = _list(inbox, changed_since=cursor)
        assert quiet["sessions"] == [] and quiet["tombstones"] == []
        fresh = _list(inbox)
        assert (quiet["total"], quiet["archived_count"]) == (fresh["total"], fresh["archived_count"])
        assert (moved["total"], moved["archived_count"]) == (fresh["total"], fresh["archived_count"])


def test_a_filter_that_was_never_read_is_read_from_the_store(inbox, mobile_home, opened):
    cursor = _list(inbox, change_cursor=1)["change_cursor"]
    opened.clear()
    archived = _list(inbox, changed_since=cursor, archived="only")
    assert opened and archived["archived_count"] == _list(inbox, archived="only")["archived_count"]
