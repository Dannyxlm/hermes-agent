"""Door boundary rejects unknown wire shapes, credentials and viewer URLs."""
import json
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import pytest
from pydantic import ValidationError

from tests.tui_gateway import test_desktop_door as helpers

pytest_plugins = ['tests.tui_gateway.test_desktop_door']


class TestDoorBoundary:
    def test_configured_viewer_base_is_exact_and_client_local(self, desktop, door):
        from tools.desktop_door_client import DoorUnavailable

        door.viewer_base_url = 'https://viewer.example:9443/shared-screen'
        door.client.viewer_base_url = door.viewer_base_url
        assert helpers.watch(desktop)['result']['url'].startswith(door.viewer_base_url + '/')
        door.viewer_base_url = 'https://other.example/shared-screen'
        with pytest.raises(DoorUnavailable):
            door.client.observe('fixture-owner', 'fixture-profile')

    def test_configured_client_reads_nonsecret_endpoint_settings(self, tmp_path, monkeypatch):
        from hermes_cli.config import atomic_config_write
        from hermes_cli import profiles
        from tools.desktop_door_client import DoorClient

        monkeypatch.setattr(profiles, '_get_default_hermes_home', lambda: tmp_path)
        credential = tmp_path / 'fixture-credential'
        atomic_config_write(tmp_path / 'config.yaml', {'cloudseed_mobile': {'desktop_door': {
            'credential_file': str(credential), 'port': 8081,
            'viewer_base_url': 'https://viewer.example:9443/shared-screen',
        }}})
        client = DoorClient.configured()
        assert client.credential_file == credential
        assert client.port == 8081
        assert client.viewer_base_url == 'https://viewer.example:9443/shared-screen'

    @pytest.mark.parametrize('change', ['unknown_field', 'boolean_epoch', 'wrong_resource', 'nonfinite_time'])
    def test_unexpected_state_never_authorizes(self, desktop, door, change):
        if change == 'unknown_field':
            door.state['new_protocol_field'] = True
        elif change == 'boolean_epoch':
            door.state['epoch'] = True
        elif change == 'wrong_resource':
            door.state['resource_id'] = 'studio'
        else:
            door.state['gate']['closed_at'] = float('nan')
        from tools.desktop_door_client import DoorUnavailable
        with pytest.raises(DoorUnavailable, match='Desktop door response unconfirmed'):
            door.client.state()
        assert helpers.watch(desktop)['error']['code'] == 4002
        assert not any(route == 'observe' for route, _ in door.calls)

    @pytest.mark.parametrize('change', ['host', 'credentials', 'path', 'input_policy', 'upstream_ticket', 'relative_base', 'foreign_base'])
    def test_viewer_url_is_exactly_scoped(self, desktop, door, change):
        from tools.desktop_door_client import DoorObservation
        body = helpers.watch(desktop)['result']
        parts = urlsplit(body['url'])
        fragment = parse_qs(parts.fragment)
        parts = {
            'host': parts._replace(netloc='other.example:8443'),
            'credentials': parts._replace(netloc='user@' + parts.netloc),
            'path': parts._replace(path='/desktop/v/other/viewer/'),
        }.get(change, parts)
        if change == 'input_policy':
            fragment.pop('view_only')
        if change == 'relative_base':
            fragment['base'] = [urlsplit(fragment['base'][0]).path]
        if change == 'foreign_base':
            fragment['base'] = ['https://other.example:8443' + urlsplit(fragment['base'][0]).path]
        if change == 'upstream_ticket':
            body['ticket'] = 'fixture-upstream-ticket'
        body['url'] = urlunsplit(parts._replace(fragment=urlencode(fragment, doseq=True)))
        with pytest.raises(ValidationError):
            DoorObservation.model_validate(body, strict=True)

    def test_service_credential_is_required_and_not_in_error(self, desktop, door):
        from tools.desktop_door_client import DoorUnavailable
        door.client.credential_file.write_text('wrong-fixture-credential', encoding='utf-8')
        with pytest.raises(DoorUnavailable) as failure:
            door.client.state()
        assert 'wrong-fixture-credential' not in str(failure.value)
        assert 'Authorization' not in str(failure.value)

    def test_post_scope_is_server_minted_not_installation(self, desktop, door):
        from tools.desktop_space_lease import digest
        assert helpers.watch(desktop)['result']['mode'] == 'watch'
        body = next(body for route, body in door.calls if route == 'observe')
        assert body == {'resource_id': 'ava-desktop', 'owner_id': desktop.scope,
                        'profile_id': digest(str(desktop.home.resolve())), 'mode': 'watch'}
        assert 'installation' not in json.dumps(body)
