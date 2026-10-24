"""Cold history authorization does not manufacture an execution route."""
import asyncio
import httpx
import pytest
from tests.gateway.test_native_http_auth import daemon, ticket, headers
from tests.gateway.test_session_mutation_retirement import create


@pytest.mark.platforms("linux")
def test_cold_import_mutation_binds_owner_across_restart(tmp_path):
    with daemon(tmp_path) as (home, descriptor), httpx.Client(base_url=descriptor['api_origin'], trust_env=False) as client:
        # Native creation remains independently authorized while cold history binds.
        asyncio.run(create(home, descriptor))
        response = client.post('/api/sessions/import', headers=headers(ticket(home, descriptor)), json={
            'request_id': 'import', 'expected_revision': 0, 'sessions': [
                {'id': 'cold', 'source': 'cli', 'messages': [{'role': 'user', 'content': 'history'}]}]})
        assert response.status_code == 200, response.text
        snapshot = client.get('/api/sessions/cold/mutation-snapshot', headers=headers(ticket(home, descriptor)))
        assert snapshot.status_code == 200, snapshot.text
        body = {'request_id': 'edit', 'expected_revision': snapshot.json()['runtime_revision'], 'title': 'Cold history'}
        edited = client.patch('/api/sessions/cold', headers=headers(ticket(home, descriptor)), json=body)
        assert edited.status_code == 200, edited.text
        stale = client.patch('/api/sessions/cold', headers=headers(ticket(home, descriptor)), json=body | {'request_id': 'competitor', 'title': 'stale'})
        assert stale.status_code == 409 and 'revision_conflict' in stale.text
    with daemon(tmp_path) as (home, descriptor), httpx.Client(base_url=descriptor['api_origin'], trust_env=False) as client:
        assert client.patch('/api/sessions/cold', headers=headers(ticket(home, descriptor)), json=body).json() == edited.json()
        assert client.get('/api/sessions/cold/mutation-snapshot', headers=headers(ticket(home, descriptor))).status_code == 200
        denied = client.patch('/api/sessions/cold', headers={'Authorization': 'Bearer normal-http-owner'}, json=body | {'request_id': 'foreign'})
        assert denied.status_code == 403, denied.text


@pytest.mark.platforms("linux")
def test_mobile_sidebar_control_preserves_owner_and_fences(tmp_path):
    """A standalone sidecar stamps this row; the dashboard forwards the login, not its UID."""
    from hermes_state import SessionDB

    home = tmp_path / 'state'
    home.mkdir(mode=0o700)
    with SessionDB(home / 'state.db') as db:
        db.create_session('mobile-row', source='mobile', user_id='basic:alice')
    alice = 'auth:v1:["basic","","alice"]'
    bob = 'auth:v1:["basic","","bob"]'
    path = '/api/sessions/mobile-row'
    with daemon(tmp_path) as (home, descriptor), httpx.Client(
            base_url=descriptor['api_origin'], trust_env=False) as client:
        def grant(subject=alice):
            return headers(ticket(home, descriptor, subject=subject))

        for index, fields in enumerate(({'pinned': True}, {'pinned': False},
                {'title': 'Mobile chat'}, {'archived': True}, {'archived': False},
                {'unread': False}, {'unread': True})):
            row = client.get(path, params={'compact': 1}, headers=grant()).json()
            body = dict(profile='default', request_id=f'edit-{index}',
                        expected_revision=row['runtime_revision'], expected_generation=row['runtime_generation'])
            edited = client.patch(path, headers=grant(), json=body | fields)
            assert edited.status_code == 200, edited.text
            foreign = client.patch(path, headers=grant(bob), json=body | fields)
            assert foreign.status_code == 403, foreign.text
            readback = client.get(path, params={'compact': 1}, headers=grant()).json()
            assert readback['runtime_revision'] == row['runtime_revision'] + 1
            for key, value in fields.items():
                if key == 'unread':
                    assert (readback['last_read_at'] == 0.0) == value
                else:
                    assert readback[key] == value
            stale = client.patch(path, headers=grant(), json=body | fields | {'request_id': f'stale-{index}'})
            assert stale.status_code == 409 and stale.json()['detail'] == 'revision_conflict'
        # Control history binds an owner, never an execution route or a new runtime generation.
        readback = client.get(path, params={'compact': 1}, headers=grant()).json()
        assert not any(readback[key] for key in ('session_key', 'chat_id', 'origin_json'))
        assert readback['runtime_generation'] == 0
        deleted = client.delete(path, headers=grant(), params=dict(profile='default', request_id='delete',
            expected_revision=readback['runtime_revision'], expected_generation=0))
        assert deleted.status_code == 200, deleted.text
        assert client.get(path, headers=grant()).status_code == 404
