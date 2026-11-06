"""Mounted cache-closure contract: exact requested IDs, trusted selected-ancestor provenance."""
import json
from urllib.parse import quote

import pytest

from hermes_state import SessionDB

pytest_plugins = ['tests.hermes_cli.test_profiles_sidebar_scope']
URL = '/api/sessions/list-membership'


def membership(client, ids, profile='default'):
    return client.get(URL, params={'profile': profile, 'ids': json.dumps(ids)})


def launch(db, child, *, lid=None, profile='default', launcher='opener', bind=True):
    lid = lid or 'thread:' + child
    db.record_launch(lid, kind='lane', launcher_profile='default',
                     launcher_session_id=launcher, child_profile=profile)
    if bind:
        db.bind_launch(lid, child_session_id=child)


def test_selected_ancestors_and_detail_agree_without_hiding_child_reads(client, profiles_on_disk):
    with SessionDB(db_path=profiles_on_disk['default'] / 'state.db') as db:
        db.create_session('opener', 'desktop')
        db.create_session('root', 'desktop')
        launch(db, 'root')
        db.end_session('root', end_reason='compression')
        db.create_session('middle', 'desktop', parent_session_id='root')
        db.end_session('middle', end_reason='compression')
        db.create_session('tip', 'desktop', parent_session_id='middle')
        db.append_message('tip', 'assistant', 'openable')
        db.bind_launch('thread:root', child_session_id='middle')
        for sid, marker in [('branch', '_branched_from'), ('reset', '_reset_from'),
                            ('delegate', '_delegate_from')]:
            db.create_session(sid, 'desktop', parent_session_id='root', model_config={marker: 'root'})
        db.create_session('tool-child', 'tool', parent_session_id='root')
        db.create_session('unselected', 'desktop', parent_session_id='middle')
        db._write_sql("UPDATE sessions SET started_at=0 WHERE id='unselected'")
        before = db._conn.total_changes
        requested = ['root', 'middle', 'tip', 'opener', 'branch', 'reset', 'delegate',
                     'tool-child', 'unselected', 'missing', 'roo']
        response = membership(client, requested)
        assert response.status_code == 200, response.text
        assert response.json() == {'profile': 'default', 'thread_session_ids': ['root', 'middle', 'tip']}
        for sid in requested[:-2]:
            for compact in (0, 1):
                detail = client.get('/api/sessions/' + sid, params={'profile': 'default', 'compact': compact})
                assert detail.status_code == 200, detail.text
                assert detail.json()['thread_parent_id'] == ('opener' if sid in {'root', 'middle', 'tip'} else None)
        messages = client.get('/api/sessions/tip/messages', params={'profile': 'default'})
        assert messages.status_code == 200, messages.text
        assert 'openable' in messages.text
        assert db._conn.total_changes == before
        assert db._read_one("SELECT child_session_id FROM work_launches WHERE launch_id='thread:root'")[0] == 'root'


def test_descendant_binding_never_relabels_human_ancestor(client, profiles_on_disk):
    with SessionDB(db_path=profiles_on_disk['default'] / 'state.db') as db:
        db.create_session('human', 'desktop')
        db.end_session('human', end_reason='compression')
        db.create_session('bound', 'desktop', parent_session_id='human')
        launch(db, 'bound', launcher='human')
    response = membership(client, ['human', 'bound'])
    assert response.status_code == 200, response.text
    assert response.json()['thread_session_ids'] == ['bound']
    assert client.get('/api/sessions/human').json()['thread_parent_id'] is None
    assert client.get('/api/sessions/bound').json()['thread_parent_id'] == 'human'


def test_only_bound_thread_prefix_with_matching_store_profile_and_launcher(client, profiles_on_disk):
    with SessionDB(db_path=profiles_on_disk['default'] / 'state.db') as db:
        ids = ['good', 'foreign', 'blank', 'whitespace', 'lane', 'unbound', 'human-title']
        for sid in ids:
            db.create_session(sid, 'desktop')
            db.set_session_title(sid, '↳ Thread teammate ' + sid)
        launch(db, 'good')
        launch(db, 'foreign', profile='worker')
        launch(db, 'blank', launcher='')
        launch(db, 'whitespace', launcher='   ')
        launch(db, 'lane', lid='lane:thread:lane')
        launch(db, 'unbound', bind=False)
        # P3: a NULL child binding must never poison a negative membership answer.
        launch(db, 'missing', bind=False)
    response = membership(client, ids + ['missing'])
    assert response.status_code == 200, response.text
    assert response.json()['thread_session_ids'] == ['good']
    for sid in ids:
        assert client.get('/api/sessions/' + sid).json()['thread_parent_id'] == ('opener' if sid == 'good' else None)


@pytest.mark.parametrize('profile', ['default', 'worker'])
def test_same_id_different_profile_and_exact_identifier_bytes(client, profiles_on_disk, profile):
    exact = '  exact,雪  '
    for name, home in profiles_on_disk.items():
        with SessionDB(db_path=home / 'state.db') as db:
            db.create_session(exact, 'desktop')
            if name == profile:
                launch(db, exact, profile=name)
    for name in profiles_on_disk:
        response = membership(client, [exact, exact.strip(), '  exact,雪'], name)
        assert response.status_code == 200, response.text
        assert response.json() == {'profile': name, 'thread_session_ids': [exact] if name == profile else []}
        for compact in (0, 1):
            detail = client.get('/api/sessions/' + quote(exact), params={'profile': name, 'compact': compact})
            assert detail.status_code == 200, detail.text
            assert detail.json()['thread_parent_id'] == ('opener' if name == profile else None)


@pytest.mark.parametrize('raw', ['null', '{}', '"id"', '[1]', '[null]', '[true]', '[""]',
                                 '["' + 'x' * 257 + '"]', '[', json.dumps(['x'] * 101), '["\\ud800"]'])
def test_invalid_ids_rejected_before_open(client, raw):
    response = client.get(URL, params={'profile': 'default', 'ids': raw})
    assert response.status_code in (400, 422), response.text


@pytest.mark.parametrize('profile', ['', ' default ', 'DEFAULT', '../worker'])
def test_profile_requires_exact_actual_name(client, profile):
    response = membership(client, [], profile)
    assert response.status_code in (400, 422), response.text


@pytest.mark.parametrize('sid', ['embedded\x00nul', 'a/quote"\\雪', 'line\nbreak'])
def test_stored_identifier_bytes_are_not_repaired(client, profiles_on_disk, sid):
    with SessionDB(db_path=profiles_on_disk['default'] / 'state.db') as db:
        db.create_session(sid, 'desktop')
        launch(db, sid)
    response = membership(client, [sid, sid + ' '])
    assert response.status_code == 200, response.text
    assert response.json()['thread_session_ids'] == [sid]


def test_required_queries_cap_and_duplicates(client, profiles_on_disk):
    assert client.get(URL, params={'ids': '[]'}).status_code == 422
    assert client.get(URL, params={'profile': 'default'}).status_code == 422
    with SessionDB(db_path=profiles_on_disk['default'] / 'state.db') as db:
        for sid in ['x' * 256, ' ']:
            db.create_session(sid, 'desktop')
            launch(db, sid)
    response = membership(client, ['x' * 256] * 99 + [' '])
    assert response.status_code == 200, response.text
    assert response.json()['thread_session_ids'] == ['x' * 256, ' ']
    assert membership(client, []).json() == {'profile': 'default', 'thread_session_ids': []}


def test_membership_shares_authenticated_read_gate(client, profiles_on_disk, monkeypatch):
    from hermes_cli import web_server
    from hermes_cli.dashboard_auth import clear_providers, register_provider
    from plugins.dashboard_auth.session_token import SessionTokenProvider
    with SessionDB(db_path=profiles_on_disk['default'] / 'state.db') as db:
        db.create_session('child', 'desktop')
        launch(db, 'child')
    clear_providers()
    register_provider(SessionTokenProvider(token='fixture-read-token'))
    monkeypatch.setattr(web_server.app.state, 'auth_required', True, raising=False)
    try:
        for path, params in [(URL, {'profile': 'default', 'ids': '["child"]'}),
                             ('/api/sessions/child', {'profile': 'default'})]:
            response = client.get(path, params=params, headers={'Authorization': 'Bearer wrong'})
            assert response.status_code == 401, response.text
            response = client.get(path, params=params, headers={'Authorization': 'Bearer fixture-read-token'})
            assert response.status_code == 200, response.text
    finally:
        clear_providers()
