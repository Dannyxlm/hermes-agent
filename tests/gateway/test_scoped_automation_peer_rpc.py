"""Review probe (PR #204 gateway lane): which grants reach canonical groups.peer.register."""
import json
from types import SimpleNamespace

import pytest

from gateway.runtime_bootstrap import _PURPOSE_CAPABILITIES
from gateway.session_identity import authenticated_subject
from tests.gateway.test_hosted_group_members import _gateway
from tui_gateway.ws import _authority_connection, _dispatch_request


def _conn(authority, purpose, subject):
    ws = SimpleNamespace(scope={'hermes.session_authority': authority})
    transport = SimpleNamespace(desktop_local=False)
    identity = {'provider': 'local', 'user_id': subject, 'profile_id': authority.profile_id,
                'instance_id': authority.instance_id, 'capabilities': _PURPOSE_CAPABILITIES[purpose],
                'native_bootstrap': True, 'bootstrap_purpose': purpose, 'profile_scope': 'profile'}
    # operator exactly as gateway/run_api.py derives it: purpose == 'interactive'
    return _authority_connection(ws, transport, identity, purpose == 'interactive'), transport


@pytest.mark.parametrize('purpose', ['team-room', 'session-wake'])
def test_scoped_automation_cannot_register_peer(tmp_path, monkeypatch, purpose):
    import gateway.session_group_controls as sgc
    out = {'purpose': purpose, 'source': sgc.__file__}
    subject = authenticated_subject({'provider': 'basic', 'user_id': 'fixture-owner'})
    legacy, probed = [], []
    with _gateway(tmp_path, monkeypatch, served=('peer',)) as (runner, _homes, call):
        authority = runner.session_authority
        monkeypatch.setattr('tui_gateway.ws.server.dispatch', lambda *a, **k: legacy.append('dispatch') or None)
        monkeypatch.setattr('tui_gateway.ws.dispatch_legacy', lambda *a, **k: legacy.append('legacy') or {
            'jsonrpc': '2.0', 'id': 1, 'error': {'code': -1, 'message': 'LEGACY-HANDLER-REACHED'}})
        connection, transport = _conn(authority, purpose, subject)

        def rpc(method, **params):
            request = {'id': 1, 'method': method, 'params': {'profile': 'default', **params}}
            return json.loads(json.dumps(call(_dispatch_request(connection, request, method, transport))))

        out['caps'] = sorted(connection.actor.capabilities)
        from gateway.hosted_room_peer import catalog_mapping
        from gateway.run import _profile_runtime_scope
        from pathlib import Path
        try:
            with _profile_runtime_scope(Path(authority.profile_id)):
                catalog = catalog_mapping(
                    installation_id='install-remote', link_modes=('direct',), persistent_process=True,
                    target_profile='default',
                    endpoint={'available': True, 'url': 'https://attacker-chosen.invalid', 'transport_security': 'tls'})
        except Exception as exc:  # report, still exercise the gate with a bogus catalog
            out['catalog_error'] = f'{type(exc).__name__}: {str(exc)[:80]}'
            catalog = {'bogus': True}
        digest = catalog.get('catalog_digest', 'a' * 64)
        if 'session:control' in connection.actor.capabilities:
            created = rpc('groups.create', room_id='probe-room', name='Probe room', members=[
                {'member_id': 'm-local', 'profile': 'default', 'handle': 'local'},
                {'member_id': 'm-peer', 'profile': 'default', 'handle': 'remote',
                 'target': {'kind': 'peer', 'peer_id': 'peer-1', 'installation_id': 'install-remote',
                            'profile': 'default', 'capability_digest': digest}}])
            out['create'] = created.get('error') or 'created'
            room = (created.get('result') or {}).get('room') or {}

            def fake_probe(self, *, grant):
                probed.append(self.base_url if hasattr(self, 'base_url') else getattr(self, '_base_url', '?'))
                return {'catalog': catalog, 'room_id': 'probe-room', 'home_install_id': room.get('authority_gateway_id'),
                        'authority_gateway_id': room.get('authority_gateway_id'),
                        'authority_epoch': room.get('authority_epoch'), 'member_id': 'm-peer', 'target_profile': 'default'}
            try:
                from tui_gateway.hosted_room_peer_http import PeerRunsHTTPClient
                monkeypatch.setattr(PeerRunsHTTPClient, 'probe', fake_probe)
            except Exception as exc:
                out['patch_error'] = repr(exc)
        reply = rpc('groups.peer.register', room_id='probe-room', member_id='m-peer',
                    target_url='https://attacker-chosen.invalid', target_profile='default', grant='caller-supplied-grant',
                    catalog=catalog)
        out['register'] = reply.get('error') or reply.get('result')
        out['probed_urls'] = probed
        service = authority.hosted_room_service
        out['route_bound'] = sorted(str(k) for k in getattr(service, 'peer_routes', {}))
        reply = rpc('groups.peer.invite', room_id='r', home_install_id='h', authority_gateway_id='h',
                    authority_epoch=1, member_id='m')
        out['invite'] = reply.get('error') or 'ISSUED'
        caps = rpc('groups.capabilities')
        out['room_link'] = (caps.get('result') or {}).get('room_link') or caps.get('error')
        out['legacy_calls'] = legacy
    assert out['register'].get('data', {}).get('reason') == 'permission_denied'
    assert out['probed_urls'] == []
    assert out['route_bound'] == []
