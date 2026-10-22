"""Cooperative CDP admission for the shared desktop, not hostile-agent isolation."""
from __future__ import annotations

import sqlite3
from urllib.parse import urlsplit


class DesktopInputDenied(PermissionError):
    pass


def is_desktop_cdp(endpoint: str) -> bool:
    try:
        parsed = urlsplit(endpoint)
        return parsed.port == 9225
    except ValueError:
        return True


def assert_agent_admitted(endpoint: str) -> int | None:
    if not is_desktop_cdp(endpoint):
        return
    from hermes_cli.profiles import _get_default_hermes_home
    from tools.desktop_space_lease import LeaseStore

    try:
        leases = LeaseStore(_get_default_hermes_home() / 'desktop-spaces.sqlite')
        lease = leases.read('desktop')
        from tools.desktop_door_client import DoorClient, DoorNotConfigured, DoorUnavailable
        try:
            door = DoorClient.configured()
        except DoorNotConfigured:
            if leases.agent_permitted('desktop', lease.epoch):
                return lease.epoch
            raise DesktopInputDenied('Desktop hand-back unconfirmed') from None
        except DoorUnavailable:
            raise DesktopInputDenied('Desktop gate configuration unavailable') from None
        try:
            state = door.state()
        except DoorUnavailable:
            raise DesktopInputDenied('Desktop gate state unavailable') from None
        if not state.agent_open():
            raise DesktopInputDenied('Desktop agent gate unconfirmed')
        from tools.desktop_control import DesktopController
        from tools.desktop_spaces import SpaceStore
        from tools.desktop_door_client import DoorConflict
        try:
            DesktopController.sync(SpaceStore(leases.path), state)
        except (DoorUnavailable, DoorConflict):
            raise DesktopInputDenied('Desktop hand-back unconfirmed') from None
        if not leases.agent_permitted('desktop', state.epoch):
            raise DesktopInputDenied('Desktop lease changed')
        return state.epoch
    except (OSError, sqlite3.Error) as exc:
        raise DesktopInputDenied('Desktop admission unavailable') from exc


def task_endpoint(task_id: str) -> str:
    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    from tools.browser_tool import _active_sessions, _last_session_key

    supervisor = SUPERVISOR_REGISTRY.get(task_id)
    if supervisor is not None:
        return str(getattr(supervisor, 'cdp_url', ''))
    return str((_active_sessions.get(_last_session_key(task_id)) or {}).get('cdp_url') or '')
