"""Bootstrap control client for real child-process probes; stdlib only."""
import json
import os
import socket


def control(home, verb, params=None):
    """Bootstrap control channel: private named pipe on Windows, AF_UNIX elsewhere."""
    request = json.dumps({'protocol': 1, 'id': 1, 'verb': verb, 'params': params or {}}).encode() + b'\n'
    if os.name == 'nt':
        from gateway.runtime_bootstrap_windows import query_runtime_control
        raw = query_runtime_control(home, request, 2)
    else:
        pointer = home / 'gateway.sock.path'
        path = pointer.read_text(encoding='utf-8').strip() if pointer.exists() else str(home / 'gateway.sock')
        with socket.socket(socket.AF_UNIX) as peer:
            peer.settimeout(2)
            peer.connect(path)
            peer.sendall(request)
            with peer.makefile('rb') as stream:
                raw = stream.readline()
    reply = json.loads(raw.split(b'\n', 1)[0])
    assert reply['ok'], reply
    return reply['result']
