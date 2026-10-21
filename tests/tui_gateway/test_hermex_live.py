"""Hermex live contracts through the public dispatcher on disposable SQLite."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport


@pytest.fixture
def live_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_load_cfg", dict)
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session("chat", "desktop")
        db.create_session("other", "desktop")
        db.create_session("hidden", "kanban")
    peer = SimpleNamespace(auth_identity={"user_id": "owner", "provider": "test"})
    token = bind_transport(peer)
    yield home
    reset_transport(token)


def rpc(method, **params):
    return server.handle_request({"id": "live-test", "method": method,
                                  "params": {"profile": "default", **params}})


def result(method, **params):
    response = rpc(method, **params)
    assert "result" in response, response
    return response["result"]


def append(home, sid, role, text):
    with SessionDB(db_path=home / "state.db") as db:
        return db.append_message(sid, role, text)


def test_catalog_first_sync_and_nested_edit(live_home):
    skill = live_home / "skills" / "live-proof"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: live-proof\ndescription: Use when testing.\n---\nRead the reference.\n")
    reference = skill / "references" / "notes.md"
    reference.parent.mkdir()
    reference.write_text("before")
    first = result("commands.catalog", include_entries=True)
    assert first["catalog_version"] == 1
    entry = next(e for e in first["entries"] if e["slash"] == "/live-proof")
    assert entry["is_new"] is None
    reference.write_text("after")
    changed = result("commands.catalog", since_revision=first["revision"])
    assert changed["revision"] != first["revision"]
    assert next(e for e in changed["entries"] if e["id"] == entry["id"])["is_new"] is True
    assert result("commands.catalog", since_revision=changed["revision"])["entries"]
    assert dict(changed["pairs"])["/live-proof"] == "Use when testing."


def test_catalog_snapshot_scope_does_not_cross_workspace(live_home):
    first = result("commands.catalog", include_entries=True)
    server._sessions["runtime"] = {"profile_home": str(live_home), "cwd": str(live_home / "project")}
    second = result("commands.catalog", session_id="runtime", since_revision=first["revision"])
    assert all(e["is_new"] is None for e in second["entries"])
    assert second["revision"] != first["revision"]


def test_search_exact_ids_repeated_snippets_and_window(live_home):
    one = append(live_home, "chat", "assistant", "😀 booking page")
    two = append(live_home, "chat", "assistant", "😀 booking page")
    found = result("session.search", query='"booking page"', session_id="chat", limit=1)
    assert len(found["results"]) == 1
    match = found["results"][0]["matches"][0]
    assert match["message_id"] == str(one)
    assert match["ranges"] == [{"start": 3, "length": 12}]
    assert found["results"][0]["match_count"] is None
    second = result("session.search", query='"booking page"', session_id="chat", limit=1,
                    cursor=found["next_cursor"])
    assert second["results"][0]["matches"][0]["message_id"] == str(two)
    window = result("session.messages.window", session_id="chat", message_id=str(two),
                    expected_revision=found["revision"], limit=1)
    assert window["anchor_message_id"] == str(two)
    assert [m["id"] for m in window["messages"]] == [two]
    assert window["offset"] is None


def test_search_old_assistant_ancestor_and_rewind(live_home):
    target = append(live_home, "chat", "assistant", "exact ancestor needle")
    with SessionDB(db_path=live_home / "state.db") as db:
        for i in range(130):
            db.append_message("chat", "user", f"filler {i}")
        db._conn.execute("UPDATE sessions SET end_reason='compression', ended_at=1 WHERE id='chat'")
        db.create_session("tip", "desktop", parent_session_id="chat")
        db.append_message("tip", "assistant", "continuation")
    page = result("session.search", query="needle", session_id="tip")
    assert page["results"][0]["lineage_root"] == "chat"
    assert page["results"][0]["matches"][0]["source_session_id"] == "chat"
    window = result("session.messages.window", session_id="tip", message_id=str(target),
                    expected_revision=page["revision"], limit=3)
    assert window["messages"][0]["id"] == target
    with SessionDB(db_path=live_home / "state.db") as db:
        db._conn.execute("UPDATE messages SET active=0, compacted=0 WHERE id=?", (target,))
    assert "error" in rpc("session.messages.window", session_id="tip", message_id=str(target),
                           expected_revision=page["revision"])
    assert not result("session.search", query="needle", session_id="tip")["results"]


def test_search_scope_and_cursor_binding(live_home):
    append(live_home, "chat", "user", "needle")
    append(live_home, "chat", "assistant", "needle two")
    append(live_home, "other", "assistant", "needle elsewhere")
    append(live_home, "hidden", "assistant", "needle internal")
    page = result("session.search", query="needle", session_id="chat", limit=1)
    assert "error" in rpc("session.search", query="elsewhere", session_id="chat", cursor=page["next_cursor"])
    assert "error" in rpc("session.search", query="needle", session_id="other", cursor=page["next_cursor"])
    token = bind_transport(SimpleNamespace(auth_identity={"user_id": "foreign", "provider": "test"}))
    try:
        assert "error" in rpc("session.search", query="needle", session_id="chat", cursor=page["next_cursor"])
    finally:
        reset_transport(token)
    assert "error" in rpc("session.messages.window", session_id="other", message_id="1",
                           expected_revision=page["revision"])
    global_page = result("session.search", query="needle")
    assert "hidden" not in {r["session_id"] for r in global_page["results"]}
    append(live_home, "chat", "user", "needle new commit")
    continued = result("session.search", query="needle", session_id="chat", cursor=page["next_cursor"])
    assert [m["message_id"] for g in continued["results"] for m in g["matches"]] == ["2"]


@pytest.mark.parametrize("method,params,code", [
    ("session.search", {"query": "needle", "limit": 101}, 4000),
    ("session.search", {"query": "needle", "limit": True}, 4000),
    ("session.search", {"query": "needle", "session_id": "missing"}, 4400),
    ("session.search", {"query": "needle", "profile": "missing"}, 4064),
    ("session.messages.window", {"session_id": "chat", "message_id": "not-id", "expected_revision": "x"}, 4000),
])
def test_invalid_scope_and_limits_fail_closed(live_home, method, params, code):
    assert rpc(method, **params)["error"]["code"] == code


def test_search_id_and_title_only_results(live_home):
    with SessionDB(db_path=live_home / "state.db") as db:
        db._conn.execute("UPDATE sessions SET title='the unusual title' WHERE id='other'")
    for query in ("other", "unusual"):
        page = result("session.search", query=query)
        row = next(r for r in page["results"] if r["session_id"] == "other")
        assert row["matches"] == []
        assert row["match_count"] is None


def test_window_adjacent_pages(live_home):
    ids = [append(live_home, "chat", "assistant", f"needle {i}") for i in range(5)]
    page = result("session.search", query="needle", session_id="chat")
    params = {"session_id": "chat", "message_id": str(ids[2]), "expected_revision": page["revision"], "limit": 2}
    middle = result("session.messages.window", **params)
    newer = result("session.messages.window", **params, cursor=middle["newer_cursor"])
    assert [m["id"] for m in newer["messages"]] == [ids[4]]
    older = result("session.messages.window", **params, cursor=middle["older_cursor"])
    assert [m["id"] for m in older["messages"]] == ids[:2]


def test_catalog_new_file_and_unknown_snapshot(live_home):
    initial = result("commands.catalog", include_entries=True)
    path = live_home / "skills" / "fresh-proof" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: fresh-proof\ndescription: Use when new.\n---\nNew skill.\n")
    changed = result("commands.catalog", since_revision=initial["revision"])
    fresh = next(e for e in changed["entries"] if e["slash"] == "/fresh-proof")
    assert fresh["is_new"] is True
    unknown = result("commands.catalog", since_revision="foreign-snapshot")
    assert all(e["is_new"] is None for e in unknown["entries"])
    assert all(e["is_new"] is None for e in changed["entries"] if e["kind"] == "command")


def test_search_and_window_are_read_only_and_profile_exact(live_home, monkeypatch):
    path = live_home / "profiles" / "ops" / "state.db"
    path.parent.mkdir(parents=True)
    with SessionDB(db_path=path) as db:
        db.create_session("chat", "desktop")
        target = db.append_message("chat", "assistant", "secondary needle")
    append(live_home, "chat", "assistant", "launch needle")
    original = SessionDB.__init__
    def read_only(self, *args, **kwargs):
        assert kwargs.get("read_only") is True
        original(self, *args, **kwargs)
    monkeypatch.setattr(SessionDB, "__init__", read_only)
    found = result("session.search", profile="ops", query="needle", session_id="chat")
    assert found["profile"] == "ops"
    assert found["results"][0]["matches"][0]["snippet"] == "secondary needle"
    window = result("session.messages.window", profile="ops", session_id="chat", message_id=str(target),
                    expected_revision=found["revision"])
    assert window["messages"][0]["content"] == "secondary needle"
    assert "error" in rpc("session.messages.window", profile="default", session_id="chat", message_id=str(target),
                           expected_revision=found["revision"])


def test_search_hides_model_only_and_wrong_owner(live_home):
    with SessionDB(db_path=live_home / "state.db") as db:
        db.append_message("chat", "assistant", "needle private scaffold", display_metadata={"model_only": True})
        db.append_message("other", "assistant", "needle foreign owner")
        db._conn.execute("UPDATE sessions SET profile_name='someone-else' WHERE id='other'")
    assert not result("session.search", query="needle")["results"]
    assert "error" in rpc("session.search", query="needle", session_id="other")


def test_dispatch_reads_leave_the_reader_thread(live_home):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    observed = threading.Event()
    frames = []
    owner = threading.get_ident()
    class Peer:
        auth_identity = {"user_id": "owner", "provider": "test"}
        def write(self, frame):
            frames.append((threading.get_ident(), frame))
            observed.set()
        def close(self):
            pass
    for name in ("commands.catalog", "session.search", "skills.changes.list", "skills.changes.get",
                 "skills.changes.decide", "session.messages.window"):
        assert name in server._LONG_HANDLERS
    with ThreadPoolExecutor(max_workers=1) as pool:
        from unittest.mock import patch
        with patch.object(server, "_pool", pool):
            response = server.dispatch({"id": "async-read", "method": "session.search",
                                        "params": {"profile": "default", "query": "needle"}}, Peer())
            assert response is None
            assert observed.wait(10)
    assert frames[0][0] != owner
    assert "result" in frames[0][1]


def test_unrelated_writer_does_not_invalidate_search_or_window(live_home):
    ids = [append(live_home, "chat", "assistant", f"needle {i}") for i in range(3)]
    page = result("session.search", query="needle", session_id="chat", limit=1)
    append(live_home, "other", "assistant", "a concurrent unrelated write")
    window = result("session.messages.window", session_id="chat", message_id=str(ids[0]),
                    expected_revision=page["revision"], limit=1)
    assert [m["id"] for m in window["messages"]] == ids[:1]
    continued = result("session.search", query="needle", session_id="chat", limit=1,
                       cursor=page["next_cursor"])
    assert continued["results"][0]["matches"][0]["message_id"] == str(ids[1])


def test_per_chat_search_first_page_ignores_global_distractors(live_home):
    with SessionDB(db_path=live_home / "state.db") as db:
        for i in range(1100):
            db.append_message("other", "assistant", f"needle elsewhere {i}")
        target = db.append_message("chat", "assistant", "needle target")
    page = result("session.search", query="needle", session_id="chat", limit=1)
    assert [m["message_id"] for g in page["results"] for m in g["matches"]] == [str(target)]


def test_older_window_partial_page_never_overlaps(live_home):
    ids = [append(live_home, "chat", "assistant", f"needle {i}") for i in range(6)]
    page = result("session.search", query="needle", session_id="chat")
    params = {"session_id": "chat", "message_id": str(ids[1]), "expected_revision": page["revision"], "limit": 3}
    first = result("session.messages.window", **params)
    older = result("session.messages.window", **params, cursor=first["older_cursor"])
    assert [m["id"] for m in first["messages"]] == ids[1:4]
    assert [m["id"] for m in older["messages"]] == ids[:1]
    assert not ({m["id"] for m in first["messages"]} & {m["id"] for m in older["messages"]})
    returned = result("session.messages.window", **params, cursor=older["newer_cursor"])
    assert [m["id"] for m in returned["messages"]] == ids[1:4]


def test_legacy_catalog_is_opt_in_and_keeps_the_skill_memo(live_home, monkeypatch):
    from agent import skill_commands
    first = result("commands.catalog")
    assert set(first) == {"pairs", "sub", "canon", "commands", "categories", "skills", "skill_count", "warning"}
    def unexpected_scan():
        raise AssertionError("warm legacy catalog rescanned skill commands")
    monkeypatch.setattr(skill_commands, "scan_skill_commands", unexpected_scan)
    assert result("commands.catalog") == first


@pytest.mark.parametrize("profile", ["ops", "does-not-exist"])
def test_catalog_live_session_retains_legacy_profile_precedence(live_home, profile):
    (live_home / "profiles" / "ops").mkdir(parents=True)
    server._sessions["runtime"] = {"profile_home": str(live_home), "cwd": str(live_home)}
    base = result("commands.catalog", session_id="runtime")
    mismatched = result("commands.catalog", session_id="runtime", profile=profile)
    assert mismatched == base
    opted = result("commands.catalog", session_id="runtime", profile=profile, since_revision="unknown")
    assert opted == base


def test_cursor_contains_no_profile_path_or_query(live_home):
    import base64
    append(live_home, "chat", "assistant", "sensitive-query one")
    append(live_home, "chat", "assistant", "sensitive-query two")
    page = result("session.search", query="sensitive-query", session_id="chat", limit=1)
    decoded = base64.urlsafe_b64decode(page["next_cursor"])
    assert str(live_home).encode() not in decoded
    assert b"sensitive-query" not in decoded


def test_invalid_lineage_has_target_error_code(live_home):
    with SessionDB(db_path=live_home / "state.db") as db:
        db._conn.execute("UPDATE sessions SET parent_session_id='other', end_reason='compression' WHERE id='chat'")
        db._conn.execute("UPDATE sessions SET parent_session_id='chat', end_reason='compression' WHERE id='other'")
    assert rpc("session.search", query="needle", session_id="chat")["error"]["code"] == 4400


def test_global_search_revision_lands_in_each_returned_conversation(live_home):
    with SessionDB(db_path=live_home / "state.db") as db:
        for i in range(8):
            sid = f"global-{i}"
            db.create_session(sid, "desktop")
            db.append_message(sid, "assistant", "needle")
    page = result("session.search", query="needle")
    assert len(page["results"]) == 8
    assert len(page["revision"]) <= 256
    append(live_home, "other", "assistant", "unrelated streaming")
    for group in page["results"]:
        anchor = group["matches"][0]["message_id"]
        window = result("session.messages.window", session_id=group["session_id"], message_id=anchor,
                        expected_revision=page["revision"])
        assert str(window["messages"][0]["id"]) == anchor


def test_legacy_tool_call_copies_keep_the_canonical_representative(live_home):
    calls = [{"id":"call-proof", "type":"function", "function":{"name":"terminal","arguments":"before"}}]
    with SessionDB(db_path=live_home / "state.db") as db:
        older = db.append_message("chat", "assistant", "needle original", timestamp=100, tool_calls=calls)
        calls[0]["function"]["arguments"] = "pruned"
        newer = db.append_message("chat", "assistant", "needle carried copy", timestamp=100, tool_calls=calls)
        db._conn.execute("UPDATE messages SET display_identity=NULL, display_order=NULL WHERE session_id='chat'")
        db._conn.execute("UPDATE messages SET active=0, compacted=1 WHERE id=?", (older,))
    found = result("session.search", query="needle", session_id="chat")
    assert [m["message_id"] for g in found["results"] for m in g["matches"]] == [str(newer)]
    window = result("session.messages.window", session_id="chat", message_id=str(newer),
                    expected_revision=found["revision"])
    assert [m["id"] for m in window["messages"]] == [newer]
