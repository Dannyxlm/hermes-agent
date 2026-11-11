"""MCP profile projection retains references, never literal credential values."""
from copy import deepcopy

import pytest

from hermes_cli.tools_config import credential_safe_mcp_server_definition


@pytest.mark.parametrize('name', ['FAKE_MCP_TOKEN', 'api_key', 'service_PASSWORD', 'AUTH',
                                  'CUSTOM_CLIENT_KEY', 'SVC_ACCESS-KEY', 'MY-TOKEN', 'SERVICE-SECRET'])
def test_secret_environment_names_preserve_only_target_scope_references(name):
    entry = {'command': 'fixture-server', 'env': {name: 'synthetic-literal', 'DEBUG': '1'}}
    original = deepcopy(entry)
    projected, required = credential_safe_mcp_server_definition(entry)
    assert projected == {'command': 'fixture-server', 'env': {'DEBUG': '1'}}
    assert required is True
    assert entry == original
    for reference in ('${TARGET_SECRET}', '${env:TARGET_SECRET}'):
        entry['env'][name] = reference
        projected, required = credential_safe_mcp_server_definition(entry)
        assert projected == entry
        assert required is True


def test_projection_removes_secret_headers_and_query_without_changing_transport():
    entry = {
        'url': 'https://fixture.invalid/mcp?api_key=synthetic-literal&query=widgets',
        'headers': {'Authorization': 'Bearer synthetic-literal',
                    'X_SERVICE_TOKEN': 'synthetic-literal', 'X-Auth-Token': 'synthetic-literal',
                    'PRIVATE-TOKEN': 'synthetic-literal', 'X-Trace': 'visible'},
        'timeout': 12,
    }
    original = deepcopy(entry)
    projected, required = credential_safe_mcp_server_definition(entry)
    assert projected == {'url': 'https://fixture.invalid/mcp?query=widgets',
                         'headers': {'X-Trace': 'visible'}, 'timeout': 12}
    assert required is True
    assert entry == original
