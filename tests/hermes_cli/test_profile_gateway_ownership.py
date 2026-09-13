"""Profile cards distinguish shared gateway service from a profile-owned process."""
from unittest.mock import patch

import pytest

from hermes_cli import profiles
from hermes_cli.web_routers.profiles import _profile_to_dict


@pytest.mark.parametrize('own,shared,default,expected_shared', [
    (False, True, False, True),
    (True, True, False, False),
    (False, False, False, False),
    (True, False, True, False),
])
def test_gateway_ownership_survives_dashboard_serialization(tmp_path, own, shared, default, expected_shared):
    with patch.object(profiles, '_check_gateway_running', return_value=own), patch.object(
        profiles, '_served_by_running_multiplexer', return_value=shared
    ):
        info = profiles._profile_info('default' if default else 'specialist', tmp_path, is_default=default)
    row = _profile_to_dict(info)
    assert row['gateway_shared'] is expected_shared
    assert row['gateway_running'] is (own or (shared and not default))
