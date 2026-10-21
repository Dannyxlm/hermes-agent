"""Publication, compatibility and dispatch regressions for the Files index."""
import copy
import contextvars
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_state import SessionDB
from plugins.cloudseed_mobile.dashboard.plugin_api import deliverable_store
from tests.tui_gateway import test_files_index as fixtures
from tui_gateway import server

indexed = fixtures.indexed
mobile_home = fixtures.mobile_home
peer = fixtures.peer
result = fixtures.result
rpc = fixtures.rpc


def refresh(index):
    for _ in range(20):
        index.refresh(max_sessions=100)
        if not index.snapshot()['refresh_pending']:
            return
    pytest.fail('fixture refresh did not converge')


def test_unpublished_source_writes_preserve_paired_snapshot(indexed, monkeypatch):
    home, _root, index, _wid = indexed
    page = result(session_id='root', limit=1)
    monkeypatch.setattr(type(index), 'refresh', lambda *args, **kwargs: None)
    with sqlite3.connect(home / 'state.db') as db:
        db.execute("UPDATE sessions SET title=title WHERE id='other'")
    paired = result('links.index', session_id='root', snapshot_id=page['snapshot_id'])
    following = result(session_id='root', limit=1, snapshot_id=page['snapshot_id'], cursor=page['next_cursor'])
    assert paired['snapshot_id'] == page['snapshot_id'] == following['snapshot_id']
    assert paired['coverage']['refresh_pending']
    assert paired['totals'] == page['totals']
    assert paired['coverage']['complete']


def test_noop_republication_preserves_snapshot(indexed):
    home, _root, index, _wid = indexed
    before = result()
    with sqlite3.connect(home / 'state.db') as db:
        db.execute("UPDATE sessions SET title=title WHERE id='other'")
    refresh(index)
    after = result(snapshot_id=before['snapshot_id'])
    assert after['index_revision'] == before['index_revision']


def test_published_changes_invalidate_snapshot(indexed):
    home, _root, index, _wid = indexed
    before = result()
    with SessionDB(db_path=home / 'state.db') as db:
        db.append_message('root', 'assistant', 'https://example.com/published')
    refresh(index)
    assert rpc('links.index', profile='default', snapshot_id=before['snapshot_id'])['error']['code'] == 4091


def test_truncated_tool_output_does_not_poison_any_counts(indexed):
    home, _root, index, _wid = indexed
    expected = result(session_id='root')['totals']
    with SessionDB(db_path=home / 'state.db') as db:
        db.append_message('other', 'tool', 'x' * (1024 * 1024 + 1), tool_name='terminal')
    refresh(index)
    assert result(session_id='root')['totals'] == expected
    page = result()
    assert page['coverage']['complete']
    assert all(value is not None for value in page['totals'].values())


def test_truncated_link_scope_leaves_files_exact(indexed):
    home, _root, index, _wid = indexed
    expected = result(session_id='root')['totals']
    with SessionDB(db_path=home / 'state.db') as db:
        db.append_message('other', 'assistant', 'x' * (1024 * 1024 + 1))
    refresh(index)
    assert result(session_id='root')['totals'] == expected
    page = result()
    assert page['totals']['files'] == expected['files']
    assert page['totals']['media'] == expected['media']
    assert page['totals']['links'] is None and page['totals']['messages'] is None


def test_legacy_payload_and_window_exclude_new_evidence(indexed):
    _home, _root, _index, _wid = indexed
    legacy = deliverable_store('default')
    refresh(legacy)
    rows = legacy.query(session_id='root')['items']
    assert [row['display_name'] for row in rows] == ['report.md']
    assert all('author_kind' not in row and 'author_profile' not in row for row in rows)
    assert all('author_kind' not in o and 'author_profile' not in o for row in rows for o in row['occurrences'])


def test_rollback_query_cannot_see_links_or_attachments(indexed):
    _home, _root, _index, _wid = indexed
    legacy = deliverable_store('default')
    refresh(legacy)
    with legacy.connection() as db:
        generation = legacy.snapshot(db)['generation']
        rows = [json.loads(r[0]) for r in db.execute('SELECT data FROM entries WHERE generation=? AND sid IN (?,?)', (generation, 'root', 'tip'))]
    assert {row['kind'] for row in rows} == {'file'}
    assert [row['display_name'] for row in rows] == ['report.md']


def test_new_attachment_ids_still_open_through_existing_byte_route(indexed):
    from plugins.cloudseed_mobile.dashboard.plugin_api import workspace_read
    attachment = next(row for row in result(session_id='root')['items'] if row['display_name'] == 'hero.png')
    response = workspace_read('default', id=attachment['id'])
    assert response['path'] == 'outputs/hero.png' and response['text'] == 'image'


def test_legacy_colon_filename_is_a_file(indexed):
    home, root, index, _wid = indexed
    (root / 'notes:v2.md').write_text('notes')
    with SessionDB(db_path=home / 'state.db') as db:
        db.append_message('root', 'assistant', '[Notes](notes:v2.md)')
    refresh(index)
    legacy = deliverable_store('default')
    refresh(legacy)
    assert 'notes:v2.md' in {row['display_name'] for row in legacy.query(session_id='root')['items']}


def test_global_feed_counts_deliveries_not_reads_or_failures(indexed):
    home, root, index, _wid = indexed
    with SessionDB(db_path=home / 'state.db') as db:
        db.append_message('root', 'assistant', '[Only read](outputs/read.md)')
        for name, outcome in [('failed.md', {'success': False}), ('made.md', {'success': True})]:
            db.append_message('root', 'assistant', '', tool_calls=[{'id': name, 'function': {'name': 'write_file', 'arguments': json.dumps({'path': str(root / 'outputs' / name)})}}])
            db.append_message('root', 'tool', json.dumps(outcome), tool_call_id=name, tool_name='write_file')
    refresh(index)
    page = result()
    assert {row['display_name'] for row in page['items']} == {'report.md', 'hero.png', 'made.md'}
    assert page['totals']['files'] == 2
    scoped = result(session_id='root')
    assert 'read.md' in {row['display_name'] for row in scoped['items']}
    assert 'failed.md' not in {row['display_name'] for row in scoped['items']}


@pytest.mark.parametrize('method', ['files.index', 'links.index'])
def test_dispatch_and_refresh_do_not_block_reader(indexed, peer, monkeypatch, method):
    home, _root, index, _wid = indexed
    entered = threading.Event()
    release = threading.Event()
    queried = threading.Event()
    real_query = __import__('tui_gateway.methods_files', fromlist=['query_index']).query_index
    def slow_refresh(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
    def query(*args, **kwargs):
        value = real_query(*args, **kwargs)
        queried.set()
        return value
    monkeypatch.setattr(type(index), 'refresh', slow_refresh)
    monkeypatch.setattr('tui_gateway.methods_files.query_index', query)
    with sqlite3.connect(home / 'state.db') as db:
        db.execute("UPDATE sessions SET title=title WHERE id='other'")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(server.dispatch, {'id': 'async-files', 'method': method, 'params': {'profile': 'default'}}, peer)
        try:
            assert queried.wait(5)
            assert future.result(timeout=2) is None
            assert entered.wait(5)
            from tests.waiting import wait_until
            assert wait_until(lambda: any(frame.get('id') == 'async-files' for frame in peer.frames), timeout=2)
            assert 'result' in next(frame for frame in peer.frames if frame.get('id') == 'async-files')
        finally:
            release.set()


def test_balanced_url_parentheses_survive(indexed):
    home, _root, index, _wid = indexed
    url = 'https://en.wikipedia.org/wiki/Foo_(bar)'
    with SessionDB(db_path=home / 'state.db') as db:
        db.append_message('root', 'assistant', url)
    refresh(index)
    assert [row['url'] for row in result('links.index', q='wikipedia')['items']] == [url]


@pytest.fixture
def live_sized(indexed):
    _home, _root, index, wid = indexed
    template = result(session_id='root')['items'][0]
    with index.connection() as db:
        generation = index.snapshot(db)['generation']
        rows = []
        for ordinal in range(10000):
            row = copy.deepcopy(template)
            row.update(id='live-sized-' + str(ordinal), display_name='synthetic-' + str(ordinal) + '.md',
                       relative_path='outputs/synthetic-' + str(ordinal) + '.md', workspace_id=wid,
                       kind='file', action='delivered', provenance='assistant_reference')
            row['occurrences'] = [{**template['occurrences'][0], 'id': str(ordinal) + '-' + str(n),
                                   'message_id': str(ordinal), 'action': 'delivered'} for n in range(2)]
            rows.append(row)
        index._stage(db, 'root', rows)
        while index._publish(db, 'root', generation, time.monotonic() + 30, None) is None:
            pass
    return indexed


def test_live_sized_evidence_completes_under_query_budget(live_sized):
    contexts = [contextvars.copy_context() for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        pages = list(executor.map(lambda ctx: ctx.run(result, limit=1), contexts))
    assert all(page['totals']['files'] >= 10000 and page['items'] for page in pages)


def test_live_sized_readers_with_active_python_projection(live_sized, monkeypatch):
    home, _root, index, _wid = live_sized
    content = json.dumps({'rows': [str(ordinal) for ordinal in range(500)]})
    with SessionDB(db_path=home / 'state.db') as db:
        db.create_session('refresh-load', 'desktop')
        for _ in range(1500):
            db.append_message('refresh-load', 'tool', content, tool_name='terminal')
    entered = threading.Event()
    original = index._project_segment
    def project(session, *args):
        if session['id'] == 'refresh-load':
            entered.set()
        return original(session, *args)
    monkeypatch.setattr(index, '_project_segment', project)
    def warm():
        for _ in range(3):
            index.refresh(max_sessions=1)
    def read():
        return [result(limit=1) for _ in range(3)]
    contexts = [contextvars.copy_context() for _ in range(3)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        writer = pool.submit(contexts[0].run, warm)
        assert entered.wait(5)
        readers = [pool.submit(ctx.run, read) for ctx in contexts[1:]]
        pages = [page for future in readers for page in future.result(timeout=20)]
        writer.result(timeout=20)
    assert all(page['items'] for page in pages)


@pytest.mark.parametrize('role,storage', [('user', 'content'), ('assistant', 'display_metadata')])
def test_attachment_coverage_tracks_only_admitted_attachment_blocks(indexed, role, storage):
    home, root, index, _wid = indexed
    blocks = [{'type': 'file', 'path': str(root / 'outputs/report.md')}] * 513
    with SessionDB(db_path=home / 'state.db') as db:
        mid = db.append_message('other', role, json.dumps(blocks) if storage == 'content' else 'No user attachment')
        if storage == 'display_metadata':
            db._conn.execute('UPDATE messages SET display_metadata=? WHERE id=?', (json.dumps({'attachments': blocks}), mid))
            db._conn.commit()
    refresh(index)
    page = result()
    if role == 'user':
        assert page['totals']['files'] is None and page['totals']['media'] is None
    else:
        assert page['totals']['files'] == 1 and page['totals']['media'] == 1
        assert page['coverage']['complete']


def test_unchanged_rebuild_retains_published_revision(indexed):
    _home, _root, index, _wid = indexed
    before = result()
    with index.connection() as db:
        db.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(index.scan_version - 1), 'scan_version'))
    refresh(index)
    after = result(snapshot_id=before['snapshot_id'])
    assert after['totals'] == before['totals'] and after['coverage']['complete']


def test_fully_scanned_empty_store_has_exact_zero_totals(indexed):
    home, _root, _index, _wid = indexed
    ops = home / 'profiles' / 'ops'
    with sqlite3.connect(ops / 'state.db') as db:
        db.execute('DELETE FROM sessions')
    with server._session_profile_runtime_scope({'profile_home': str(ops)}, hydrate_secrets=False):
        index = deliverable_store('ops', files_index=True)
        refresh(index)
    page = rpc('files.index', profile='ops')['result']
    assert page['totals'] == dict(media=0, files=0, links=0, messages=0)
    assert page['coverage']['complete']


def test_legacy_raw_wal_signature_remains_compatible(indexed):
    from fastapi import BackgroundTasks
    from plugins.cloudseed_mobile.dashboard.plugin_api import deliverable_list
    from plugins.cloudseed_mobile.workspace_files import digest
    home, _root, _index, _wid = indexed
    legacy = deliverable_store('default')
    refresh(legacy)
    with sqlite3.connect(home / 'state.db') as source:
        source.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    values = []
    for name in ('state.db', 'state.db-wal'):
        path = home / name
        try:
            info = path.stat()
            values.append((name, info.st_ino, info.st_size, info.st_mtime_ns))
        except FileNotFoundError:
            values.append((name, None))
    with legacy.connection() as db:
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('source_signature', json.dumps(digest(values))))
    response = deliverable_list(BackgroundTasks(), 'default')
    assert not response['coverage']['refresh_pending'] and response['refresh_status'] == 'ready'


def test_unknown_profile_is_profile_denial(indexed):
    assert rpc('files.index', profile='missing')['error']['code'] == 4403


def test_workspace_suggestion_cap_reports_incomplete_coverage(indexed):
    _home, root, _index, wid = indexed
    for ordinal in range(201):
        (root / ('child-' + str(ordinal) + '.txt')).write_text('child')
    page = result(workspace_id=wid, include_workspace=True, limit=200)
    assert not page['coverage']['complete']
    assert page['totals'] == dict(media=1, files=1, links=3, messages=4)


def test_generation_only_roots_are_not_directory_browse_grants(indexed):
    from plugins.cloudseed_mobile import workspace_files
    home, _root, _index, _wid = indexed
    generated = home.parent / 'generation-only'
    generated.mkdir()
    (generated / 'result.txt').write_text('generated')
    config = home / 'config.yaml'
    config.write_text(config.read_text() + '  generation_roots:\n    - ' + str(generated) + '\n')
    wid = next(w['id'] for w in workspace_files.workspaces('default', include_generation=True)['items'] if w['root_path'] == str(generated))
    assert rpc('files.index', profile='default', workspace_id=wid, include_workspace=True)['error']['code'] == 4403


def test_delegate_truncation_does_not_poison_public_counts(indexed):
    home, _root, index, _wid = indexed
    before = result()['totals']
    with SessionDB(db_path=home / 'state.db') as db:
        db.append_message('delegate', 'assistant', 'x' * (1024 * 1024 + 1))
    refresh(index)
    page = result()
    assert page['totals'] == before and page['coverage']['complete']


@pytest.mark.parametrize('rebuild', [False, True])
def test_totals_unknown_until_current_scan_pass_completes(indexed, monkeypatch, rebuild):
    from plugins.cloudseed_mobile.links_index import workspace_counts
    _home, _root, index, wid = indexed
    expected = result()['totals']
    expected_workspace = workspace_counts(index)[wid]
    advance = index.refresh
    monkeypatch.setattr(type(index), 'refresh', lambda *args, **kwargs: None)
    if rebuild:
        with index.connection() as db:
            db.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(index.scan_version - 1), 'scan_version'))
    else:
        index.path.unlink()
    partial_publications = 0
    for _ in range(30):
        meta = index.snapshot()
        completed = bool(meta.get('source_signature') and meta.get('scan_version') == index.scan_version)
        for method in ('files.index', 'links.index'):
            page = result(method)
            assert page['totals'] == (expected if completed else dict(media=None, files=None, links=None, messages=None))
            assert page['coverage']['complete'] is completed
        assert workspace_counts(index)[wid] == (expected_workspace if completed else None)
        if completed:
            assert partial_publications > 0
            return
        partial_publications += bool(meta.get('index_revision') and meta.get('indexed_sessions'))
        advance(max_sessions=1)
    pytest.fail('one-session slices did not complete the current scan pass')


def test_dashboard_warmer_builds_mixed_index_without_rpc_reads(indexed):
    from plugins.cloudseed_mobile.dashboard.plugin_api import warm_indexes
    from plugins.cloudseed_mobile.deliverables_warmer import DeliverablesWarmer
    _home, _root, index, _wid = indexed
    expected = result()['totals']
    index.path.unlink()
    warmer = DeliverablesWarmer(warm_indexes, max_sessions=1)
    for _ in range(30):
        warmer.run_once()
        if not index.snapshot()['warm_pending']:
            break
    assert index.snapshot().get('source_signature'), 'dashboard warmer must complete the mixed index without a Files open'
    assert result()['totals'] == expected


@pytest.mark.parametrize('filters', [{'q': 'example.com'}, {'q': 'unextracted'}, {'author': 'me'}, {'author': 'agents'}])
def test_filtered_queries_keep_uncertainty_from_unmatched_sessions(indexed, filters):
    home, root, index, _wid = indexed
    with SessionDB(db_path=home / 'state.db') as db:
        db.create_session('uncertain', 'desktop', cwd=str(root))
        db.append_message('uncertain', 'user', 'x' * (1024 * 1024 + 1) + ' https://example.com/unextracted')
    refresh(index)
    page = result('links.index', **filters)
    assert page['totals']['links'] is None and page['totals']['messages'] is None
    assert page['totals']['files'] is not None and page['totals']['media'] is not None
    assert not page['coverage']['complete']
    assert result(session_id='root', **filters)['coverage']['complete']


def test_workspace_coverage_includes_entries_outside_session_cwd(indexed):
    home, root, index, wid = indexed
    with SessionDB(db_path=home / 'state.db') as db:
        db.create_session('cross-workspace', 'desktop', cwd=str(home.parent / 'elsewhere'))
        db.append_message('cross-workspace', 'assistant', 'MEDIA: ' + str(root / 'outputs/report.md'))
        db.append_message('cross-workspace', 'user', 'x' * (1024 * 1024 + 1))
    refresh(index)
    page = result(workspace_id=wid)
    assert any(row['stored_session_id'] == 'cross-workspace' for row in page['items'])
    assert page['totals']['links'] is None and page['totals']['messages'] is None
    assert page['totals']['files'] == 1 and page['totals']['media'] == 1
    assert not page['coverage']['complete']
    assert result(session_id='root', workspace_id=wid)['coverage']['complete']
