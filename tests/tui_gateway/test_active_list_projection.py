"""Read-only active-list projections: canonical scopes, bounded work and query counts."""
import logging
import sqlite3

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_methods_mobile import mobile_home, peer, rpc
from tui_gateway import server
from tui_gateway.mobile_push_payloads import Scope
from tui_gateway.mobile_push_store import PushStore


def store(home):
    return PushStore(home / "mobile-push" / "outbox.sqlite3", clock=lambda: 123)


def test_canonical_run_and_expected_absent_scopes_are_quiet(mobile_home, peer, caplog, monkeypatch):
    monkeypatch.setattr(server, "_resolve_model", lambda: "fixture")
    push = store(mobile_home)
    try:
        push.start_run(Scope("native", "ops", "ops-root"), run_id="bot-run")
        server._sessions.update({
            "bot": {"session_key": "ops-root", "profile_home": str(mobile_home / "profiles" / "ops")},
            "draft": {"history": []},
            "stale": {"session_key": "gone", "_finalized": True},
        })
        caplog.set_level(logging.WARNING, logger="tui_gateway.inbox_summaries")
        rows = rpc("session.active_list")["result"]["sessions"]
        assert next(row for row in rows if row["id"] == "bot")["latest_run"] == {
            "run_id": "bot-run", "status": "starting", "at": 123}
        assert {row["id"] for row in rows} == {"bot", "draft"}
        assert not [r for r in caplog.records if "Inbox run summary unavailable" in r.message]
    finally:
        push.close()


@pytest.mark.parametrize('compacted', [False, True])
def test_active_list_excludes_rewound_replies(mobile_home, peer, monkeypatch, compacted):
    monkeypatch.setattr(server, '_resolve_model', lambda: 'fixture')
    with SessionDB(db_path=mobile_home / 'state.db') as db:
        db.create_session('ordinary', 'desktop')
        kept = db.append_message('ordinary', 'assistant', 'retained reply', timestamp=10)
        if compacted:
            db._write_sql('UPDATE messages SET active=0, compacted=1 WHERE id=?', (kept,))
        revoked = db.append_message('ordinary', 'assistant', 'rewound reply', timestamp=20)
        db.deactivate_message('ordinary', revoked)
        db._write_sql("UPDATE sessions SET last_read_at=15 WHERE id='ordinary'")
        assert [m['id'] for m in db.get_messages('ordinary', include_compacted=True)] == [kept]
    server._sessions['live'] = {'session_key': 'ordinary', 'history': []}
    row = rpc('session.active_list')['result']['sessions'][0]
    assert row['last_assistant_reply'] == {
        'row_id': kept, 'at': 10, 'preview': 'retained reply', 'unread': False}
    with SessionDB(db_path=mobile_home / 'state.db') as db:
        db.deactivate_message('ordinary', kept)
        db._write_sql('UPDATE messages SET compacted=0 WHERE id=?', (kept,))
    assert 'last_assistant_reply' not in rpc('session.active_list')['result']['sessions'][0]


def test_unknown_stored_id_warns_once_per_session_interval(mobile_home, peer, monkeypatch, caplog):
    import time
    monkeypatch.setattr(server, '_resolve_model', lambda: 'fixture')
    now = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    server._sessions['unknown'] = {'session_key': 'not-stored', 'pending_title': 'Draft title'}
    push = store(mobile_home)
    try:
        caplog.set_level(logging.WARNING, logger='tui_gateway.inbox_summaries')
        for _ in range(5):
            rows = rpc('session.active_list')['result']['sessions']
            assert rows[0]['title'] == 'Draft title' and 'latest_run' not in rows[0]
        warnings = lambda: [r for r in caplog.records if 'Inbox run summary unavailable' in r.message]
        assert len(warnings()) == 1
        assert 'not-stored' not in warnings()[0].message
        now[0] += 61
        rpc('session.active_list')
        assert len(warnings()) == 2
    finally:
        push.close()


def test_preview_reads_only_a_bounded_tail(mobile_home, peer, monkeypatch):
    monkeypatch.setattr(server, '_resolve_model', lambda: 'fixture')
    inspected = []
    preview = server._notice_preview_text
    def counted(msg):
        inspected.append(1)
        return preview(msg)
    monkeypatch.setattr(server, '_notice_preview_text', counted)
    server._sessions['draft'] = {'history': [{'role': 'user', 'content': 'old'}] + [
        {'role': 'user', 'content': 'hidden', 'display_kind': 'hidden'}] * 10000}
    row = rpc('session.active_list')['result']['sessions'][0]
    assert row['preview'] == ''
    assert len(inspected) == 64


def test_batched_destinations_match_selected_compression_paths(mobile_home, peer, monkeypatch):
    from tui_gateway.mobile_session_scope import projection_destinations
    monkeypatch.setattr(server, '_resolve_model', lambda: 'fixture')
    home = mobile_home / 'profiles' / 'ops'
    with SessionDB(db_path=home / 'state.db') as db:
        db._write_sql("UPDATE sessions SET end_reason='compression', ended_at=100 WHERE id='ops-root'")
        db.create_session('middle', 'desktop', parent_session_id='ops-root')
        db._write_sql("UPDATE sessions SET end_reason='compression', ended_at=101 WHERE id='middle'")
        db.create_session('tip', 'desktop', parent_session_id='middle')
        db.create_session('stale', 'desktop', parent_session_id='ops-root')
        db._write_sql("UPDATE sessions SET end_reason='ws_orphan_reap', ended_at=102 WHERE id='stale'")
        for key, cfg, source in [('branch', {'_branched_from': 'ops-root'}, 'desktop'),
                                 ('reset', {'_reset_from': 'ops-root'}, 'desktop'),
                                 ('delegate', {'_delegate_from': 'ops-root'}, 'desktop'),
                                 ('tool', {}, 'tool')]:
            db.create_session(key, source, parent_session_id='ops-root', model_config=cfg)
        ids = ['ops-root', 'middle', 'tip', 'stale', 'branch', 'reset', 'delegate', 'tool']
        expected = {key: db.get_compression_lineage(key) for key in ids}
        projected = projection_destinations(db, 'ops', ids)
        assert {key: value['root']['_lineage_ids'] for key, value in projected.items()} == expected
        assert projected['tip']['scope'] == Scope('native', 'ops', 'ops-root')
        assert projected['branch']['scope'] == Scope('native_session', 'ops', 'branch')


class NoCopyHistory(list):
    def __iter__(self):
        raise AssertionError('active_list copied the full history')


def test_active_list_preview_does_not_copy_history(mobile_home, peer, monkeypatch):
    monkeypatch.setattr(server, "_resolve_model", lambda: "fixture")
    history = NoCopyHistory([{'role': 'user', 'content': 'old'}] * 10000 + [
        {'role': 'assistant', 'content': '  latest ordinary\nreply  '},
        {'role': 'user', 'content': 'machinery', 'display_kind': 'hidden'},
    ])
    server._sessions['draft'] = {'history': history}
    try:
        row = rpc('session.active_list')['result']['sessions'][0]
        assert row['preview'] == 'latest ordinary reply'
        assert row['message_count'] == 10002
    finally:
        server._sessions['draft']['history'] = []


@pytest.mark.parametrize("n", [1, 10, 50])
def test_active_list_database_work_is_constant(mobile_home, peer, monkeypatch, record_property, n):
    monkeypatch.setattr(server, "_resolve_model", lambda: "fixture")
    push = store(mobile_home)
    try:
        with SessionDB(db_path=mobile_home / "state.db") as db:
            for i in range(n):
                key = f"ordinary-{i}"
                db.create_session(key, "desktop")
                db.set_session_title(key, f"Title {i}")
                db.append_message(key, "assistant", "durable reply", timestamp=100)
                push.start_run(Scope("native_session", "default", key), run_id=f"run-{i}")
                server._sessions[key] = {"session_key": key, "history": [{"role": "assistant", "content": "same preview"}]}
        connections, statements = [], []
        connect = sqlite3.connect
        def counted(*args, **kwargs):
            db = connect(*args, **kwargs)
            connections.append(1)
            db.set_trace_callback(lambda sql: statements.append(sql))
            return db
        monkeypatch.setattr(sqlite3, "connect", counted)
        rows = rpc("session.active_list")["result"]["sessions"]
        print(f"ACTIVE_LIST_COUNTS N={n} connections={len(connections)} statements={len(statements)}")
        record_property('live_rows', n)
        record_property('connections', len(connections))
        record_property('statements', len(statements))
        assert len(rows) == n
        assert all(row["preview"] == "same preview" and row["last_assistant_reply"]["preview"] == "durable reply" for row in rows)
        assert all(row["title"] == f"Title {i}" and row["latest_run"]["run_id"] == f"run-{i}" for i, row in enumerate(rows))
        assert len(connections) <= 2
        assert len(statements) <= 20
    finally:
        push.close()
