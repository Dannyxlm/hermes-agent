"""Credentialed loopback client for the Desktop door control contract."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from typing import Literal


class DoorUnavailable(RuntimeError):
    pass


class DoorRefused(DoorUnavailable):
    """An authenticated HTTP response definitively refused the operation."""


class DoorTransportError(DoorUnavailable):
    pass


class DoorProtocolError(DoorUnavailable):
    pass


class DoorNotConfigured(DoorUnavailable):
    pass


class DoorConflict(ValueError):
    pass


class Wire(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class Gate(Wire):
    state: Literal['open', 'closed']
    open_upstream: int = Field(ge=0)
    closed_at: float | None
    last_client_frame_at: float | None

    def drained(self):
        return (self.state == 'closed' and self.open_upstream == 0 and self.closed_at is not None
                and (self.last_client_frame_at is None or self.last_client_frame_at <= self.closed_at))


class Session(Wire):
    viewer_id: str = Field(min_length=1, max_length=256)
    owner_id: str
    profile_id: str
    mode: Literal['watch', 'control']
    expires_at: float
    lease_epoch: int = Field(ge=0)


class DoorState(Wire):
    resource_id: Literal['ava-desktop']
    holder: Literal['agent', 'human']
    epoch: int = Field(ge=0)
    state: Literal['WATCHING', 'REQUESTED', 'DRAINING', 'HUMAN', 'HANDING_BACK']
    ready: bool
    unavailable_reason: str | None
    gate: Gate
    sessions: list[Session]
    human_viewer_id: str | None
    expires_at: float | None

    def agent_open(self):
        return (self.holder == 'agent' and self.state == 'WATCHING' and self.gate.state == 'open'
                and self.human_viewer_id is None and self.expires_at is None
                and not any(session.mode == 'control' for session in self.sessions))


class DoorObservation(Wire):
    ticket: str = Field(min_length=1, max_length=256)
    viewer_id: str = Field(min_length=1, max_length=256, pattern=r'^[A-Za-z0-9_-]+$')
    url: str
    expires_at: float
    mode: Literal['watch', 'control']
    lease_epoch: int = Field(ge=0)

    @model_validator(mode='after')
    def viewer_url(self):
        parsed = urlsplit(self.url)
        parts = parse_qs(parsed.fragment, strict_parsing=True)
        prefix = 'v' if self.mode == 'watch' else 'c'
        base = f'/ava-desktop/{prefix}/{self.viewer_id}'
        if (self.ticket != self.viewer_id or parsed.scheme != 'https'
                or parsed.hostname != 'ava-cloud.tailbe2d4b.ts.net' or parsed.port != 3443
                or parsed.username is not None or parsed.password is not None or parsed.query
                or parsed.path != base + '/viewer/'
                or parts.get('ticket') != [self.viewer_id]
                or parts.get('base') != [base]
                or parts.get('audio') != ['off'] or parts.get('scale') != ['fit']
                or set(parts) != ({'ticket', 'base', 'audio', 'scale', 'view_only'} if self.mode == 'watch'
                                  else {'ticket', 'base', 'audio', 'scale'})
                or (self.mode == 'watch' and parts.get('view_only') != ['1'])):
            raise ValueError('Invalid door viewer URL')
        return self


class DoorAck(Wire):
    accepted: Literal[True]
    holder: Literal['agent', 'human']
    epoch: int = Field(ge=0)
    viewer_id: str | None
    expires_at: float | None
    gate: Gate


class DoorClient:
    def __init__(self, credential_file: Path, *, port: int = 3216):
        self.credential_file = Path(credential_file)
        self.port = port

    @classmethod
    def configured(cls):
        from hermes_cli.config import load_config_readonly
        from hermes_cli.profiles import _get_default_hermes_home
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override

        token = set_hermes_home_override(_get_default_hermes_home())
        try:
            mobile = load_config_readonly().get('cloudseed_mobile', {})
            if not isinstance(mobile, dict):
                raise DoorUnavailable('Desktop door configuration invalid')
            cfg = mobile.get('desktop_door')
        except (OSError, ValueError):
            raise DoorUnavailable('Desktop door configuration unavailable') from None
        finally:
            reset_hermes_home_override(token)
        if cfg is None:
            raise DoorNotConfigured('Desktop door is not configured')
        if not isinstance(cfg, dict):
            raise DoorUnavailable('Desktop door configuration invalid')
        path = cfg.get('credential_file')
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise DoorUnavailable('Desktop door credential path invalid')
        return cls(Path(path))

    def _request(self, route: str, body: dict | None, model):
        conn = None
        try:
            credential = self.credential_file.read_text(encoding='utf-8-sig').strip()
            if not credential or len(credential) > 4096 or any(ch.isspace() for ch in credential):
                raise DoorUnavailable('Desktop door credential unavailable')
            conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=7)
            headers = {'Authorization': 'Bearer ' + credential, 'Content-Type': 'application/json'}
            payload = json.dumps(body, allow_nan=False).encode() if body is not None else None
            conn.request('POST' if body is not None else 'GET', '/control/' + route, payload, headers)
            response = conn.getresponse()
            raw = response.read(262145)
            if response.status in (409, 410):
                raise DoorConflict('Desktop viewer changed')
            if response.status != 200:
                raise DoorRefused('Desktop door unavailable')
            if len(raw) > 262144:
                raise DoorProtocolError('Desktop door response unconfirmed')
            return model.model_validate(json.loads(raw), strict=True)
        except (OSError, http.client.HTTPException):
            raise DoorTransportError('Desktop door unavailable') from None
        except (ValueError, ValidationError) as exc:
            if isinstance(exc, DoorConflict):
                raise
            raise DoorProtocolError('Desktop door response unconfirmed') from None
        finally:
            if conn is not None:
                conn.close()

    def state(self) -> DoorState:
        return self._request('state', None, DoorState)

    def observe(self, scope: str, profile: str) -> DoorObservation:
        return self._request('observe', self._body(scope, profile, mode='watch'), DoorObservation)

    def renew(self, scope: str, profile: str, viewer: str, epoch: int) -> DoorObservation:
        return self._request('renew', self._body(scope, profile, viewer_id=viewer, lease_epoch=epoch), DoorObservation)

    def transition(self, verb: str, scope: str, profile: str, viewer: str, epoch: int, action_id: str) -> DoorAck:
        if verb not in ('takeover', 'handback'):
            raise ValueError('Invalid door transition')
        return self._request(verb, self._body(scope, profile, viewer_id=viewer,
                                            lease_epoch=epoch, action_id=action_id), DoorAck)

    @staticmethod
    def _body(scope, profile, **extra):
        return {'resource_id': 'ava-desktop', 'owner_id': scope, 'profile_id': profile, **extra}
