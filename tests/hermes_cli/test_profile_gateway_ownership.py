"""Profile cards distinguish shared gateway service from a profile-owned process."""
from unittest.mock import patch

import pytest

from hermes_cli import profiles, web_server_gateway
from hermes_cli.web_routers.profiles import _profile_to_dict


# Liveness answers "running" for a served profile on the multiplexer's PID (#97120), so `live` is
# True whenever `served` is; only a separate gateway (`own`) makes a served profile self-hosted.
@pytest.mark.parametrize('live,served,own,default,expected_shared,expected_running', [
    (True, True, False, False, True, True),      # served by the multiplexer, no gateway of its own
    (True, True, True, False, False, True),      # --force-started separate gateway beside it
    (True, False, True, False, False, True),     # standalone gateway, no multiplexer
    (False, False, False, False, False, False),  # stopped
    (True, False, True, True, False, True),      # default profile is the multiplexer itself
])
def test_gateway_ownership_survives_dashboard_serialization(
        tmp_path, live, served, own, default, expected_shared, expected_running):
    with patch.object(profiles, '_check_gateway_running', return_value=live), patch.object(
        profiles, '_served_by_running_multiplexer', return_value=served
    ), patch.object(web_server_gateway, '_has_own_gateway', return_value=own):
        info = profiles._profile_info('default' if default else 'specialist', tmp_path, is_default=default)
    row = _profile_to_dict(info)
    assert row['gateway_shared'] is expected_shared
    assert row['gateway_running'] is expected_running
