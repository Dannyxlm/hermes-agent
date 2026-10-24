"""The suite's network guard, shared by conftest and its own tests."""
from __future__ import annotations

import socket
import threading

# Captured at import, before any test patches them.
REAL_CONNECT = socket.socket.connect
REAL_SOCKETPAIR = socket.socketpair
_state = threading.local()


def install(monkeypatch, socketpair=REAL_SOCKETPAIR) -> None:
    """Refuse every outbound connect, except inside ``socket.socketpair()``.

    On Windows ``socketpair()`` is built from a TCP connect on 127.0.0.1, and the
    Proactor event loop (FastAPI's TestClient) needs one for its self-pipe. The
    exemption lives only for the duration of that call, on that thread, so a
    loopback connect from product code still fails the suite.
    """
    def connect(sock, address, *args, **kwargs):
        if getattr(_state, "in_socketpair", False):
            return REAL_CONNECT(sock, address, *args, **kwargs)
        raise AssertionError("Tests may not access the network")

    def refuse(*args, **kwargs):
        raise AssertionError("Tests may not access the network")

    def guarded_socketpair(*args, **kwargs):
        _state.in_socketpair = True
        try:
            return socketpair(*args, **kwargs)
        finally:
            _state.in_socketpair = False

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "socketpair", guarded_socketpair)
