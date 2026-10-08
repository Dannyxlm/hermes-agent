"""Transport liveness + build capability probes (``methods_voice.py`` hosts them)."""

from __future__ import annotations

from .base import Params, Result
from .registry import method


class PingParams(Params):
    pass


class PingResult(Result):
    pong: bool


method("ping", params=PingParams, result=PingResult,
       doc="Cheapest liveness probe; answered on the WS reader thread even while every agent is mid-turn.")


class GatewayCapabilitiesResult(Result):
    per_session_exclusive_submit: bool


method("gateway.capabilities", params=PingParams, result=GatewayCapabilitiesResult,
       doc="What THIS build enforces (a client withholds a feature unless advertised).")


class ClientCapabilitiesParams(Params):
    #: The client answers server→client requests (clarify, approval, sudo, …) — with a result or a -32601
    #: error for methods it has no handler for. A WebSocket client that never says so is treated as a
    #: build older than server→client requests and every such request fails fast for it.
    server_requests: bool = False
    #: The client consumes per-scope ``sessions.changed`` cursor frames (``profile``, ``cursor``,
    #: ``changed``, ``tombstoned``) at most every 250 ms, and the payload-free frame is no longer
    #: sent to it. Send only when ``mobile.capabilities`` lists ``session_change_cursor``.
    session_change_cursor: bool = False


class ClientCapabilitiesResult(Result):
    #: Server→client request methods this backend may send.
    server_requests: list[str]
    #: This backend counts a ``4404`` "no window here shows this session" error from a window-owned
    #: bridge (preview/terminal/window read, preview act, tour) as that client declining, not as the
    #: answer: the request settles only once every attached client declined (server_requests.py).
    #: An older backend settles on the first error, so a client sends the decline only when this is true.
    declines_not_shown: bool = False
    #: Version of the ``sessions.changed`` cursor protocol this backend speaks (0 = none).
    session_change_cursor: int = 0


method("client.capabilities", params=ClientCapabilitiesParams, result=ClientCapabilitiesResult,
       doc="What the calling client handles, sent once per connection (after gateway.ready); returns the "
           "server→client request methods this backend may send.")
