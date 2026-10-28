"""A missing door endpoint is an RPC-local error, not a gateway startup error."""
from unittest.mock import Mock

import pytest

pytest_plugins = ['tests.tui_gateway.test_desktop_takeover']


@pytest.mark.parametrize('fields', [{}, {'port': 8081}, {'viewer_base_url': 'https://viewer.example.test/desktop'}])
def test_partial_door_configuration_does_not_break_gateway(desktop, monkeypatch, fields):
    from hermes_cli.config import atomic_config_write
    from tui_gateway import server

    atomic_config_write(desktop.home / 'config.yaml', {'cloudseed_mobile': {
        'owner_provider': 'fixture', 'owner_user_id': 'owner',
        'desktop_door': {'credential_file': str(desktop.home / 'credential'), **fields},
    }})
    connection = Mock(side_effect=AssertionError('must not open HTTPConnection'))
    monkeypatch.setattr('http.client.HTTPConnection', connection)
    result = desktop.call('desktop.snapshot')
    assert result['error']['code'] == 4002
    for key in {'port', 'viewer_base_url'} - fields.keys():
        assert f'cloudseed_mobile.desktop_door.{key}' in result['error']['message']
    assert server.handle_request({'jsonrpc': '2.0', 'id': 'ping', 'method': 'ping', 'params': {}})['result'] == {'pong': True}
    connection.assert_not_called()
