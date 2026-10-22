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


def assert_agent_admitted(endpoint: str) -> None:
    if not is_desktop_cdp(endpoint):
        return
    from hermes_cli.profiles import _get_default_hermes_home
    from tools.desktop_space_lease import LeaseStore

    try:
        leases = LeaseStore(_get_default_hermes_home() / 'desktop-spaces.sqlite')
        lease = leases.read('desktop')
        if not leases.agent_permitted('desktop', lease.epoch):
            raise DesktopInputDenied('A human holds Desktop control or hand-back is unconfirmed')
        from tools.desktop_door_client import DoorClient, DoorNotConfigured, DoorUnavailable
        try:
            door = DoorClient.configured()
        except DoorNotConfigured:
            return
        try:
            state = door.state()
        except DoorUnavailable:
            raise DesktopInputDenied('Desktop gate state unavailable') from None
        if not state.agent_open() or state.epoch != lease.epoch:
            raise DesktopInputDenied('Desktop agent gate unconfirmed')
    except (OSError, sqlite3.Error) as exc:
        raise DesktopInputDenied('Desktop admission unavailable') from exc


def task_endpoint(task_id: str) -> str:
    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    from tools.browser_tool import _active_sessions, _last_session_key

    supervisor = SUPERVISOR_REGISTRY.get(task_id)
    if supervisor is not None:
        return str(getattr(supervisor, 'cdp_url', ''))
    return str((_active_sessions.get(_last_session_key(task_id)) or {}).get('cdp_url') or '')
