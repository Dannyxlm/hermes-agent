"""Tool-created threads belong in Family, not the paginated human Chats feeds."""
import pytest

from hermes_state import SessionDB

pytest_plugins = ['tests.hermes_cli.test_profiles_sidebar_scope']


def seed(home, profile):
    with SessionDB(db_path=home / 'state.db') as db:
        for i, sid in enumerate(['parent', 'human-teammate', 'arrow-only', 'foreign', 'ordinary-lane', 'child']):
            db.create_session(sid, 'desktop')
            db.set_session_title(sid, '↳ Human title' if sid == 'arrow-only' else sid)
            db.append_message(sid, 'user', 'fixture message', timestamp=100 + i)
            db._write_sql('UPDATE sessions SET started_at=? WHERE id=?', (100 + i, sid))
        for sid, launch, child_profile in [('child', 'thread:child', profile),
                                           ('foreign', 'thread:foreign', 'unrelated'),
                                           ('ordinary-lane', 'lane:ordinary', profile)]:
            db.record_launch(launch_id=launch, kind='teammate', launcher_profile='default',
                             launcher_session_id='parent', child_profile=child_profile)
            db.bind_launch(launch, child_session_id=sid)
        db.record_launch(launch_id='thread:pending', kind='lane', launcher_profile='default',
                         launcher_session_id='parent', child_profile=profile)
        db.set_session_pinned('child', True)
        db._write_sql("UPDATE sessions SET end_reason='compression' WHERE id='child'")
        db.create_session('child-tip', 'desktop', parent_session_id='child')
        db.append_message('child-tip', 'assistant', 'still openable', timestamp=200)
        db.create_session('human-branch', 'desktop', parent_session_id='child',
                          model_config={'_branched_from': 'child'})
        db.append_message('human-branch', 'user', 'owner branch', timestamp=150)
        db.create_session('human-reset', 'desktop', parent_session_id='child',
                          model_config={'_reset_from': 'child'})
        db.append_message('human-reset', 'user', 'owner reset', timestamp=160)
        db.bind_launch('thread:child', child_session_id='child-tip')
        assert db._read_one("SELECT child_session_id FROM work_launches WHERE launch_id='thread:child'")[0] == 'child'
    return {'parent', 'human-teammate', 'arrow-only', 'foreign', 'ordinary-lane', 'human-branch', 'human-reset'}


@pytest.mark.parametrize('profile', ['default', 'worker'])
@pytest.mark.parametrize('route', ['/api/sessions', '/api/profiles/sessions'])
def test_chats_filter_before_paging_and_totals(client, profiles_on_disk, route, profile):
    expected = seed(profiles_on_disk[profile], profile)
    rows = []
    for offset in range(len(expected)):
        response = client.get(route, params={'profile': profile, 'limit': 1, 'offset': offset, 'order': 'recent'})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['total'] == len(expected)
        assert len(payload['sessions']) == 1
        rows.extend(row['id'] for row in payload['sessions'])
    assert set(rows) == expected
    assert len(rows) == len(set(rows))
    with SessionDB(db_path=profiles_on_disk[profile] / 'state.db', read_only=True) as db:
        assert db.get_session('child')['archived'] == 0
        assert db.get_messages('child-tip')[0]['content'] == 'still openable'
        assert db.get_compression_tip('child') == 'child-tip'
        assert db._read_one("SELECT launcher_session_id FROM work_launches WHERE launch_id='thread:child'")[0] == 'parent'


@pytest.mark.parametrize('profile', ['default', 'worker'])
def test_desktop_sidebar_excludes_thread_even_when_pinned(client, profiles_on_disk, profile):
    expected = seed(profiles_on_disk[profile], profile)
    response = client.get('/api/profiles/sessions/sidebar', params={'recents_profile': profile, 'recents_limit': 50})
    assert response.status_code == 200, response.text
    assert {row['id'] for row in response.json()['recents']['sessions']} == expected
    tree = client.get('/api/profiles/projects/tree', params={'profile': profile})
    assert tree.status_code == 200, tree.text
    assert tree.json()['errors'] == []
    assert sum(project['sessionCount'] for project in tree.json()['projects']) == len(expected)
    assert {row['id'] for project in tree.json()['projects']
            for row in project['previewSessions']} <= expected


@pytest.mark.parametrize('route', ['/api/sessions', '/api/profiles/sessions'])
def test_child_binding_does_not_classify_a_human_ancestor(client, profiles_on_disk, route):
    with SessionDB(db_path=profiles_on_disk['default'] / 'state.db') as db:
        db.create_session('human-parent', 'desktop')
        db.append_message('human-parent', 'user', 'human conversation')
        db.end_session('human-parent', end_reason='compression')
        # Malformed/imported lineage is not evidence that a human ancestor was tool-created.
        db.create_session('bound-child', 'desktop', parent_session_id='human-parent')
        db.append_message('bound-child', 'assistant', 'continuation')
        db.record_launch(launch_id='thread:bound-child', kind='lane', launcher_profile='default',
                         launcher_session_id='human-parent', child_profile='default')
        db.bind_launch('thread:bound-child', child_session_id='bound-child')
    response = client.get(route, params={'profile': 'default', 'full': True})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['total'] == 1
    assert len(payload['sessions']) == 1
    assert payload['sessions'][0]['_lineage_root_id'] == 'human-parent'
