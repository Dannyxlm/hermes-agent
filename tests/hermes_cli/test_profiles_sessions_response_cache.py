"""Session response reuse must preserve wire bytes and next-call freshness."""

import inspect
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient


@pytest.fixture
def stores(tmp_path, monkeypatch):
    from hermes_cli import profiles
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = get_hermes_home()
    root.mkdir(parents=True, exist_ok=True)
    name = "worker-" + tmp_path.name
    home = root / "profiles" / name
    home.mkdir(parents=True)
    for directory in (root, home):
        (directory / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: root)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: root / "profiles")
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    db = SessionDB(db_path=home / "state.db")
    for sid, timestamp in (("first", 900), ("other", 800), ("older", 700)):
        db.create_session(sid, source="desktop")
        db.append_message(session_id=sid, role="user", content="héllo " + sid)
        db._conn.execute("UPDATE sessions SET started_at=?, last_activity_at=? WHERE id=?",
                         (timestamp, timestamp, sid))
        db._conn.execute("UPDATE messages SET timestamp=? WHERE session_id=?", (timestamp, sid))
    db.create_session("child", source="subagent", parent_session_id="first",
                      model_config={"_delegate_from": "first"})
    db.append_message(session_id="child", role="user", content="child preview")
    db._conn.commit()
    db.close()
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    return SimpleNamespace(root=root, home=home, name=name, clock=clock)


@pytest.fixture(params=["sessions", "sidebar"])
def route(request, stores):
    from hermes_cli.web_routers import profiles

    sidebar = request.param == "sidebar"
    path = "/api/profiles/sessions" + ("/sidebar" if sidebar else "")
    handler = profiles.get_profiles_sessions_sidebar if sidebar else profiles.get_profiles_sessions
    params = ({"recents_profile": stores.name, "recents_limit": 1,
               "recents_exclude": "cron,subagent"} if sidebar else
              {"profile": stores.name, "limit": 1, "exclude_sources": "cron,subagent"})
    app = FastAPI()
    app.include_router(profiles.sessions_router)
    raw_app = FastAPI()
    raw_app.add_api_route(path, inspect.unwrap(handler), methods=["GET"])
    with TestClient(app) as client, TestClient(raw_app) as oracle:
        yield SimpleNamespace(client=client, oracle=oracle, path=path, params=params,
                              sidebar=sidebar)


def _get(route, *, raw=False, **overrides):
    response = (route.oracle if raw else route.client).get(
        route.path, params={**route.params, **overrides})
    assert response.status_code == 200, response.text
    return response


def _rows(route, response):
    payload = response.json()
    return payload["recents"]["sessions"] if route.sidebar else payload["sessions"]


def _sql(stores, statement, values=()):
    with sqlite3.connect(stores.home / "state.db") as conn:
        conn.execute(statement, values)


def _mutate(stores, mutation, monkeypatch):
    from hermes_cli.config import atomic_config_write
    from hermes_state import SessionDB

    db_actions = {
        "archive": ("set_session_archived", ("first", True), {}),
        "pin": ("set_session_pinned", ("older", True), {}),
        "rename": ("set_session_title", ("first", "renamed café"), {}),
        "delete": ("delete_session", ("first",), {}),
        "message": ("append_message", (), {"session_id": "other", "role": "user", "content": "new message"}),
    }
    if mutation in db_actions:
        db = SessionDB(db_path=stores.home / "state.db")
        try:
            method, args, kwargs = db_actions[mutation]
            result = getattr(db, method)(*args, **kwargs)
            if mutation != "message":
                assert result
        finally:
            db.close()
        return
    policy_actions = {
        "config": lambda: atomic_config_write(stores.home / "config.yaml", {"sessions": {"show_subagents": True}}),
        "effective_config": lambda: monkeypatch.setenv("SIDEBAR_TEST_SUBAGENTS", "true"),
    }
    if mutation in policy_actions:
        policy_actions[mutation]()
        return
    statements = {
        "usage": "UPDATE sessions SET input_tokens=37, actual_cost_usd=0 WHERE id='first'",
        "heartbeat": "UPDATE sessions SET last_activity_at=1100, last_activity_kind='tool' WHERE id='other'",
        "preview": "UPDATE messages SET content='corrected preview' WHERE session_id='first'",
    }
    if mutation in statements:
        _sql(stores, statements[mutation])
        return
    if mutation == "same_mtime":
        path = stores.home / "state.db"
        before = path.stat()
        _sql(stores, "UPDATE messages SET content=replace(content,'héllo','salut') WHERE session_id='first'")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert path.stat().st_size == before.st_size
        assert path.stat().st_mtime_ns == before.st_mtime_ns
        return
    if mutation == "receipt":
        before = json.dumps({"session_id": "owner", "lineage": ["first", "other"]})
        after = json.dumps({"session_id": "owner", "lineage": ["older", "other"]})
        assert len(before) == len(after)
        _sql(stores, "UPDATE state_meta SET value=? WHERE key='gateway.local_policy.v1:owner'", (after,))
        return
    raise AssertionError(mutation)


@pytest.mark.parametrize("mutation", ["archive", "pin", "rename", "delete", "message", "config",
                                      "effective_config", "usage", "heartbeat", "preview",
                                      "same_mtime", "receipt"])
def test_next_call_observes_mutation(route, stores, mutation, monkeypatch):
    if mutation == "effective_config":
        from hermes_cli.config import atomic_config_write
        monkeypatch.setenv("SIDEBAR_TEST_SUBAGENTS", "false")
        atomic_config_write(stores.home / "config.yaml",
                            {"sessions": {"show_subagents": "${SIDEBAR_TEST_SUBAGENTS}"}})
    if mutation == "receipt":
        _sql(stores, "INSERT INTO state_meta(key,value) VALUES(?,?)",
             ("gateway.local_policy.v1:owner",
              json.dumps({"session_id": "owner", "lineage": ["first", "other"]})))
        _sql(stores, "UPDATE sessions SET end_reason='session_reset' WHERE id IN ('first','older')")
        route.params.update({"recents_limit": 10} if route.sidebar else {"limit": 10})
    warm = _get(route)
    assert warm.content == _get(route).content
    if mutation == "heartbeat":
        assert [row["id"] for row in _rows(route, warm)] == ["first"]
    _mutate(stores, mutation, monkeypatch)
    changed = _get(route)
    expected = _get(route, raw=True)
    assert expected.content != warm.content, mutation
    assert changed.content == expected.content, mutation
    assert changed.content == _get(route).content
    if mutation == "heartbeat":
        assert [row["id"] for row in _rows(route, changed)] == ["other"]


def test_unchanged_request_skips_reads_and_serialization(route, stores, monkeypatch):
    from hermes_cli.web_routers import profiles
    from starlette.responses import JSONResponse

    warm = _get(route)
    assert warm.content == _get(route, raw=True).content

    def forbidden(*args, **kwargs):
        raise AssertionError("unchanged response rebuilt")

    monkeypatch.setattr(profiles, "_read_profile_db", forbidden)
    monkeypatch.setattr(profiles, "_tag_rows", forbidden)
    monkeypatch.setattr(JSONResponse, "render", forbidden)
    hit = _get(route)
    assert hit.content == warm.content
    assert hit.headers["content-type"] == warm.headers["content-type"] == "application/json"
    assert hit.status_code == warm.status_code


@pytest.mark.parametrize("change", ["missing", "create", "replace", "roster_add", "roster_rename", "roster_remove"])
def test_store_and_roster_identity_changes(route, stores, change):
    from hermes_state import SessionDB

    if change.startswith("roster"):
        route.params.update({"recents_profile": "all"} if route.sidebar else {"profile": "all"})
        # Keep the blind base cache independent of the other all-profile test cases.
        if route.sidebar:
            route.params["messaging_limit"] = 101 + ["roster_add", "roster_rename", "roster_remove"].index(change)
    path = stores.home / "state.db"
    if change == "create":
        path.unlink()
    def replace_store():
        replacement = stores.root / "replacement.db"
        db = SessionDB(db_path=replacement)
        db.create_session("replacement", source="desktop")
        db.append_message(session_id="replacement", role="user", content="replacement")
        db.close()
        replacement.replace(path)

    def add_store():
        new_home = stores.root / "profiles" / "added"
        new_home.mkdir()
        (new_home / "config.yaml").write_text("{}\n")
        db = SessionDB(db_path=new_home / "state.db")
        db.create_session("added", source="desktop")
        db.append_message(session_id="added", role="user", content="added")
        db.close()

    def remove_store():
        (stores.home / ".deleted").write_text("deleted")
        # Removing the only identity markers is sufficient to leave the served roster.
        path.unlink()
        (stores.home / "config.yaml").unlink()

    warm = _get(route)
    actions = {
        "missing": path.unlink,
        "create": replace_store,
        "replace": replace_store,
        "roster_add": add_store,
        "roster_rename": lambda: stores.home.rename(stores.home.with_name("renamed")),
        "roster_remove": remove_store,
    }
    actions[change]()
    changed = _get(route)
    expected = _get(route, raw=True)
    assert expected.content != warm.content
    assert changed.content == expected.content


@pytest.mark.parametrize("clock", [1200.0, 600.0])
def test_activity_expiry_and_backward_clock(route, stores, clock):
    warm = _get(route)
    assert _rows(route, warm)[0]["is_active"] is True
    stores.clock[0] = clock
    result = _get(route)
    assert result.content == _get(route, raw=True).content
    if clock == 1200:
        assert _rows(route, result)[0]["is_active"] is False
    else:
        stores.clock[0] = 1200
        assert _rows(route, _get(route))[0]["is_active"] is False


def test_full_mode_is_fresh_and_preserves_blobs(stores):
    from hermes_cli.web_routers import profiles

    app = FastAPI()
    app.include_router(profiles.sessions_router)
    params = {"profile": stores.name, "full": 1}
    with TestClient(app) as client:
        warm = client.get("/api/profiles/sessions", params=params)
        assert "model_config" in warm.json()["sessions"][0]
        _sql(stores, "UPDATE sessions SET model_config=? WHERE id='first'", ('{"note":"full"}',))
        changed = client.get("/api/profiles/sessions", params=params)
        assert changed.content != warm.content
        row = next(row for row in changed.json()["sessions"] if row["id"] == "first")
        assert json.loads(row["model_config"])["note"] == "full"


def test_storage_state_and_error_recovery(route, stores, monkeypatch):
    from hermes_cli.web_routers import profiles
    from hermes_state_health import mark_storage_corrupt, reset_storage_state

    warm = _get(route)
    mark_storage_corrupt(stores.home / "state.db", "test damage")
    try:
        corrupt = _get(route)
        assert corrupt.content != warm.content
        assert corrupt.json()["storage"] == {stores.name: "corrupt"}
    finally:
        reset_storage_state(stores.home / "state.db")
    assert _get(route).content == warm.content
    original = profiles._open_session_db_at_path
    _sql(stores, "UPDATE sessions SET title='after failure' WHERE id='first'")
    profiles._sidebar_profile_cache_clear()

    def fail(*args, **kwargs):
        raise RuntimeError("temporary read failure")

    monkeypatch.setattr(profiles, "_open_session_db_at_path", fail)
    failed = _get(route)
    assert failed.json()["errors"]
    monkeypatch.setattr(profiles, "_open_session_db_at_path", original)
    recovered = _get(route)
    assert recovered.json()["errors"] == []
    assert recovered.content == _get(route, raw=True).content


def test_wal_commit_invalidates_next_call(route, stores):
    with sqlite3.connect(stores.home / "state.db") as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        warm = _get(route)
        before = (stores.home / "state.db").stat()
        writer.execute("UPDATE sessions SET title='WAL update' WHERE id='first'")
        writer.commit()
        assert (stores.home / "state.db").stat().st_mtime_ns == before.st_mtime_ns
        result = _get(route)
        assert result.content != warm.content
        assert result.content == _get(route, raw=True).content


def test_each_hit_has_an_independent_response(route, monkeypatch):
    from starlette.responses import Response

    sent = []
    original = Response.__call__

    async def record(response, *args, **kwargs):
        sent.append(response)
        await original(response, *args, **kwargs)

    monkeypatch.setattr(Response, "__call__", record)
    first = _get(route)
    sent[-1].headers["x-mutated"] = "not shared"
    second = _get(route)
    assert first.content == second.content
    assert sent[0] is not sent[1]
    assert "x-mutated" not in second.headers


def test_all_returned_activity_deadlines_are_observed(route, stores):
    route.params.update({"recents_limit": 10} if route.sidebar else {"limit": 10})
    warm = _get(route)
    assert next(row for row in _rows(route, warm) if row["id"] == "other")["is_active"]
    stores.clock[0] = 1100
    result = _get(route)
    assert not next(row for row in _rows(route, result) if row["id"] == "other")["is_active"]
    assert next(row for row in _rows(route, result) if row["id"] == "first")["is_active"]
    assert result.content == _get(route, raw=True).content
    stores.clock[0] = 1400
    assert not any(row["is_active"] for row in _rows(route, _get(route)))
    stores.clock[0] = 600
    assert all(row["is_active"] for row in _rows(route, _get(route)))


def test_build_race_is_not_published_as_stable(route, stores, monkeypatch):
    from hermes_cli.web_routers import profiles

    original = profiles._tag_rows
    mutated = False

    def mutate_after_read(*args, **kwargs):
        nonlocal mutated
        rows = original(*args, **kwargs)
        if not mutated:
            mutated = True
            _sql(stores, "UPDATE sessions SET title='raced update' WHERE id='first'")
        return rows

    monkeypatch.setattr(profiles, "_tag_rows", mutate_after_read)
    _get(route)
    result = _get(route)
    assert _rows(route, result)[0]["title"] == "raced update"
    assert result.content == _get(route, raw=True).content


@pytest.mark.parametrize("mutate_waiting", [False, True])
def test_single_flight_rechecks_after_waiting(route, stores, monkeypatch, mutate_waiting):
    from hermes_cli.web_routers import profiles

    entered = threading.Event()
    checked = threading.Event()
    release = threading.Event()
    tag = profiles._tag_rows
    tags = []

    def blocked_tag(*args, **kwargs):
        rows = tag(*args, **kwargs)
        tags.append(rows)
        if not entered.is_set():
            entered.set()
            assert release.wait(30)
        return rows

    seam = "_profile_targets" if route.sidebar else "_cron_profile_home"
    resolve = getattr(profiles, seam)

    def checked_targets(*args, **kwargs):
        targets = resolve(*args, **kwargs)
        if entered.is_set() and not release.is_set():
            checked.set()
        return targets

    monkeypatch.setattr(profiles, "_tag_rows", blocked_tag)
    monkeypatch.setattr(profiles, seam, checked_targets)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_get, route)
        assert entered.wait(30)
        second = pool.submit(_get, route)
        try:
            assert checked.wait(30)
            if mutate_waiting:
                _sql(stores, "UPDATE sessions SET title='while waiting' WHERE id='first'")
        finally:
            release.set()
        first_response, second_response = first.result(30), second.result(30)
    if mutate_waiting:
        assert _rows(route, second_response)[0]["title"] == "while waiting"
    else:
        assert first_response.content == second_response.content
        assert len(tags) == (3 if route.sidebar else 1)


@pytest.mark.parametrize("budget", ["entries", "bytes", "oversized"])
def test_cache_budgets_force_rebuild_without_changing_wire_bytes(route, stores, monkeypatch, budget):
    from hermes_cli.web_routers import profiles

    module = sys.modules.get("hermes_cli.web_routers.profile_sessions_cache")
    if module is not None:
        monkeypatch.setattr(module, "_MAX_ENTRIES", 1 if budget == "entries" else 32)
    warm = _get(route)
    if module is not None and budget != "entries":
        monkeypatch.setattr(module, "_MAX_BODY_BYTES", 1 if budget == "oversized" else len(warm.content) + 10)
    if budget == "oversized":
        _sql(stores, "UPDATE sessions SET title='oversized' WHERE id='first'")
        warm = _get(route)
    else:
        if route.sidebar:
            _get(route, cron_limit=2)
        else:
            _get(route, order="created")
    tags = []
    original = profiles._tag_rows

    def record(*args, **kwargs):
        tags.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(profiles, "_tag_rows", record)
    result = _get(route)
    assert tags
    assert result.content == warm.content


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 1, "offset": 1}, {"archived": "only"},
    {"archived": "include"}, {"order": "created"}, {"source": "desktop"},
    {"sources": "desktop, subagent"}, {"exclude_sources": " desktop, cron "},
    {"min_messages": 2},
])
def test_bound_arguments_separate_flat_wire_responses(stores, params):
    from hermes_cli.web_routers import profiles

    app = FastAPI()
    app.include_router(profiles.sessions_router)
    raw = FastAPI()
    raw.add_api_route("/api/profiles/sessions", inspect.unwrap(profiles.get_profiles_sessions))
    with TestClient(app) as client, TestClient(raw) as oracle:
        query = {"profile": stores.name}
        assert client.get("/api/profiles/sessions", params=query).status_code == 200
        query.update(params)
        expected = oracle.get("/api/profiles/sessions", params=query)
        assert client.get("/api/profiles/sessions", params=query).content == expected.content
        assert client.get("/api/profiles/sessions", params=query).content == expected.content
