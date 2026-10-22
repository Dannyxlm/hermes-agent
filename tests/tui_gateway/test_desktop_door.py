"""Credentialed fake-door contract tests. No desktop or browser connections."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest

pytest_plugins = ['tests.tui_gateway.test_desktop_takeover']


@pytest.fixture
def door(desktop, tmp_path, monkeypatch):
    from tools.desktop_door_client import DoorClient

    state = {'resource_id': 'ava-desktop', 'holder': 'agent', 'epoch': 0, 'state': 'WATCHING',
             'ready': True, 'unavailable_reason': None, 'gate': {'state': 'open', 'open_upstream': 0,
             'closed_at': None, 'last_client_frame_at': None}, 'sessions': [],
             'human_viewer_id': None, 'expires_at': None}
    control = SimpleNamespace(state=state, fail=False, invalid=False, lost_ack=False, calls=[])
    credential = tmp_path / 'door-credential'
    credential.write_text('fixture-only-credential')

    def observation(mode):
        prefix = 'v' if mode == 'watch' else 'c'
        ticket = 'viewer' if mode == 'watch' else 'c' * 43
        base = f'/ava-desktop/{prefix}/{ticket}'
        fragment = {'ticket': ticket, 'base': base, 'audio': 'off', 'scale': 'fit'}
        if mode == 'watch':
            fragment['view_only'] = '1'
        return {'ticket': ticket, 'viewer_id': 'viewer',
                'url': 'https://ava-cloud.tailbe2d4b.ts.net:3443' + base + '/viewer/#' + urlencode(fragment),
                'expires_at': time.time() + 120, 'mode': mode, 'lease_epoch': state['epoch']}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond(None)

        def do_POST(self):
            self.respond(json.loads(self.rfile.read(int(self.headers['Content-Length']))))

        def respond(self, body):
            route = self.path.rsplit('/', 1)[-1]
            control.calls.append((route, body))
            if self.headers.get('Authorization') != 'Bearer fixture-only-credential':
                self.send_error(403)
                return
            if route == 'takeover' and getattr(control, 'refused', 0):
                self.send_error(control.refused)
                return
            if control.fail:
                self.send_error(503)
                return
            if control.invalid:
                result = {'unexpected': True}
            elif route == 'state':
                result = copy.deepcopy(state)
            elif route == 'observe':
                result = observation('watch')
                state['sessions'] = [{'viewer_id': 'viewer', 'owner_id': body['owner_id'],
                    'profile_id': body['profile_id'], 'mode': 'watch', 'expires_at': result['expires_at'],
                    'lease_epoch': state['epoch']}]
            elif route == 'renew':
                result = observation('control')
                state['expires_at'] = result['expires_at']
                state['sessions'][0]['expires_at'] = result['expires_at']
            else:
                human = route == 'takeover'
                state.update(holder='human' if human else 'agent', state='HUMAN' if human else 'WATCHING',
                             epoch=state['epoch'] + 1, human_viewer_id='viewer' if human else None,
                             expires_at=time.time() + 120 if human else None)
                state['gate'] = {'state': 'closed' if human else 'open', 'open_upstream': 0,
                                 'closed_at': time.time(), 'last_client_frame_at': None}
                if human:
                    state['sessions'][0].update(mode='control', lease_epoch=state['epoch'], expires_at=state['expires_at'])
                else:
                    state['sessions'] = []
                result = {'accepted': True, 'holder': state['holder'], 'epoch': state['epoch'],
                          'viewer_id': state['human_viewer_id'], 'expires_at': state['expires_at'], 'gate': state['gate']}
                if getattr(control, 'bad_ack', False):
                    result = {'accepted': True, 'unexpected': True}
                if control.lost_ack:
                    self.close_connection = True
                    return
            payload = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    client = DoorClient(credential, port=httpd.server_port)
    monkeypatch.setattr(DoorClient, 'configured', classmethod(lambda cls: client))
    control.client = client
    try:
        yield control
    finally:
        httpd.shutdown()
        thread.join(5)
        httpd.server_close()


def action(d, verb, action_id='decision', **extra):
    state = d.call('desktop.snapshot')['result']
    return d.call('desktop.action', action=verb, action_id=action_id,
                  expected_revision=state['revision'], expected_lease_epoch=state['lease']['epoch'], **extra)


def watch(d):
    state = d.call('desktop.snapshot')['result']
    return d.call('desktop.observe', mode='watch', expected_lease_epoch=state['lease']['epoch'])


class TestDesktopDoorControl:
    def test_takeover_renew_handback_and_reconnect(self, desktop, door):
        d = desktop
        observed = watch(d)
        assert observed['result']['mode'] == 'watch'
        taken = action(d, 'take_over', viewer_id='viewer')['result']
        assert taken['status'] == 'accepted'
        epoch = taken['lease_epoch']
        assert not d.store.leases.agent_permitted('desktop', epoch)
        control = d.call('desktop.observe', mode='control', viewer_id='viewer', expected_lease_epoch=epoch)['result']
        assert control['mode'] == 'control' and '/c/' + control['ticket'] + '/' in control['url']
        assert d.call('desktop.observe', mode='control', viewer_id='other', expected_lease_epoch=epoch)['error']['code'] == 4000
        back = action(d, 'hand_back', action_id='back', viewer_id='viewer')['result']
        assert back['status'] == 'accepted' and back['lease_epoch'] == epoch + 1
        assert d.store.leases.agent_permitted('desktop', epoch + 1)
        assert watch(d)['result']['mode'] == 'watch'

    @pytest.mark.parametrize('failure', ['fail', 'invalid'])
    def test_door_unavailable_never_changes_lease(self, desktop, door, failure):
        d = desktop
        assert watch(d)['result']['viewer_id'] == 'viewer'
        before = d.store.leases.read('desktop')
        setattr(door, failure, True)
        reply = action(d, 'take_over', viewer_id='viewer')
        assert reply['error']['code'] == 4002
        assert d.store.leases.read('desktop') == before

    def test_control_capability_is_transient_not_the_persisted_viewer(self, desktop, door, caplog):
        assert watch(desktop)['result']['viewer_id'] == 'viewer'
        receipt = action(desktop, 'take_over', viewer_id='viewer')['result']
        assert receipt['status'] == 'accepted'
        observed = desktop.call('desktop.observe', mode='control', viewer_id='viewer',
                                expected_lease_epoch=receipt['lease_epoch'])
        assert 'result' in observed
        control = observed['result']
        assert control['viewer_id'] == 'viewer'
        assert control['ticket'] != control['viewer_id']
        assert '/c/' + control['ticket'] + '/' in control['url']
        assert control['ticket'] not in json.dumps(receipt) + caplog.text
        assert control['ticket'].encode() not in (desktop.home / 'desktop-spaces.sqlite').read_bytes()
        with desktop.store.leases.connection() as db:
            row = db.execute('SELECT viewer_id FROM desktop_operations WHERE action_id=?', ('decision',)).fetchone()
            from tools.desktop_space_lease import digest
            assert row['viewer_id'] == digest('viewer')

    def test_takeover_operation_retains_only_viewer_digest(self, desktop, door):
        from tools.desktop_space_lease import digest
        assert watch(desktop)['result']['viewer_id'] == 'viewer'
        assert action(desktop, 'take_over', viewer_id='viewer')['result']['status'] == 'accepted'
        with desktop.store.leases.connection() as db:
            row = db.execute('SELECT viewer_id FROM desktop_operations').fetchone()
            assert row['viewer_id'] == digest('viewer')

    @pytest.mark.parametrize('status,code', [(503, 4002), (409, 4000)])
    def test_definitively_refused_takeover_is_denied(self, desktop, door, status, code):
        assert watch(desktop)['result']['viewer_id'] == 'viewer'
        before = desktop.store.leases.read('desktop')
        door.refused = status
        reply = action(desktop, 'take_over', viewer_id='viewer')
        assert reply['error']['code'] == code
        assert desktop.store.leases.read('desktop') == before
        receipt = desktop.call('desktop.action_status', action_id='decision')['result']
        assert receipt['status'] == 'denied'
        door.refused = 0
        assert action(desktop, 'take_over', action_id='next', viewer_id='viewer')['result']['status'] == 'accepted'
        assert desktop.call('desktop.action_status', action_id='decision')['result'] == receipt

    def test_lost_ack_reconciles_without_second_transition(self, desktop, door):
        assert watch(desktop)['result']['viewer_id'] == 'viewer'
        door.lost_ack = True
        reply = action(desktop, 'take_over', viewer_id='viewer')
        receipt = reply['result']
        assert receipt['status'] == 'accepted'
        assert desktop.call('desktop.action_status', action_id='decision')['result'] == receipt
        assert sum(route == 'takeover' for route, _ in door.calls) == 1

    def test_gate_not_drained_never_accepts_control(self, desktop, door):
        assert watch(desktop)['result']['mode'] == 'watch'
        door.state['gate']['state'] = 'closed'
        door.state['gate']['open_upstream'] = 1
        reply = action(desktop, 'take_over', viewer_id='viewer')
        assert reply['error']['code'] == 4002
        assert not any(route == 'takeover' for route, _ in door.calls)


class TestDesktopDoorRecovery:
    def test_unknown_ack_is_unavailable_not_lost_transport(self, desktop, door):
        assert watch(desktop)['result']['mode'] == 'watch'
        before = desktop.store.leases.read('desktop')
        door.bad_ack = True
        reply = action(desktop, 'take_over', viewer_id='viewer')
        assert reply['error']['code'] == 4002
        assert desktop.store.leases.read('desktop') == before

    def test_watch_stays_available_when_control_not_ready(self, desktop, door):
        door.state['ready'] = False
        door.state['unavailable_reason'] = 'fixture gate disabled'
        assert watch(desktop)['result']['mode'] == 'watch'
        assert action(desktop, 'take_over', viewer_id='viewer')['error']['code'] == 4002
        assert 'control' in desktop.call('desktop.snapshot')['result']['unavailable']

    def test_foreign_profile_cannot_use_viewer(self, desktop, door):
        assert watch(desktop)['result']['mode'] == 'watch'
        state = desktop.call('desktop.snapshot', profile='secondary')['result']
        reply = desktop.call('desktop.action', profile='secondary', action='take_over', action_id='foreign',
                             viewer_id='viewer', expected_revision=state['revision'], expected_lease_epoch=0)
        assert reply['error']['code'] == 4000
        assert not any(route == 'takeover' for route, _ in door.calls)

    def test_lost_handback_ack_advances_once(self, desktop, door):
        assert watch(desktop)['result']['mode'] == 'watch'
        assert action(desktop, 'take_over', viewer_id='viewer')['result']['status'] == 'accepted'
        door.lost_ack = True
        receipt = action(desktop, 'hand_back', action_id='back', viewer_id='viewer')['result']
        assert receipt['status'] == 'accepted' and receipt['lease_epoch'] == 2
        assert desktop.call('desktop.action_status', action_id='back')['result'] == receipt
        assert sum(route == 'handback' for route, _ in door.calls) == 1

    def test_confirmed_door_restart_invalidates_old_viewer(self, desktop, door):
        assert watch(desktop)['result']['mode'] == 'watch'
        assert action(desktop, 'take_over', viewer_id='viewer')['result']['status'] == 'accepted'
        door.state.update(holder='agent', epoch=100, state='WATCHING', human_viewer_id=None,
                          expires_at=None, sessions=[])
        door.state['gate']['state'] = 'open'
        state = desktop.call('desktop.snapshot')['result']
        assert state['lease']['epoch'] == 100 and state['lease']['holder'] == 'agent'
        assert desktop.call('desktop.observe', mode='control', viewer_id='viewer', expected_lease_epoch=1)['error']['code'] == 4000
        assert desktop.store.leases.agent_permitted('desktop', 100)


class TestDesktopSavedLogin:
    def publish(self, d, *, expires_at=None):
        d.store.publish_signin('desktop', d.scope, request_id='req', revision=1,
                               origin='https://example.com', reason='Continue',
                               expires_at=expires_at or time.time() + 60, login_handle='vault_fixture',
                               masked_identifier='d•••@example.com', task_id='task', target_id='target')

    @pytest.mark.parametrize('change,status', [({'login_handle': 'other'}, 'conflict'),
        ({'request_revision': 2}, 'conflict'), ({'request_id': 'other'}, 'conflict'), ({}, 'accepted')])
    def test_exact_request_fills_once_and_resend_is_content_free(self, desktop, door, monkeypatch, change, status, caplog):
        from tools import browser_vault_signin
        d = desktop
        self.publish(d)
        fill = Mock(return_value=True)
        monkeypatch.setattr(browser_vault_signin, 'fill_requested_login', fill)
        state = d.call('desktop.snapshot')['result']
        fields = {'action': 'saved_login', 'action_id': 'saved', 'expected_revision': state['revision'],
                  'expected_lease_epoch': 0, 'request_id': 'req', 'request_revision': 1,
                  'login_handle': 'vault_fixture', **change}
        receipt = d.call('desktop.action', **fields)['result']
        assert receipt['status'] == status
        assert d.call('desktop.action', **fields)['result'] == receipt
        assert d.call('desktop.action_status', action_id='saved', installation_id='second')['result'] == receipt
        assert fill.call_count == (1 if status == 'accepted' else 0)
        assert set(receipt) == {'action_id', 'status', 'revision', 'lease_epoch', 'server_time'}
        assert 'vault_fixture' not in caplog.text

    def test_expired_request_never_fills(self, desktop, door, monkeypatch):
        from tools import browser_vault_signin
        self.publish(desktop, expires_at=time.time() - 1)
        fill = Mock()
        monkeypatch.setattr(browser_vault_signin, 'fill_requested_login', fill)
        result = action(desktop, 'saved_login', request_id='req', request_revision=1,
                        login_handle='vault_fixture')['result']
        assert result['status'] == 'expired'
        fill.assert_not_called()
