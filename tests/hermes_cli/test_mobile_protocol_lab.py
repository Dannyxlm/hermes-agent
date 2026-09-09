"""Native auth and two-client protocol proof over real loopback HTTP/WebSockets."""
import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx

from scripts.mobile_protocol_lab import verify_protocol


def test_real_native_protocol_and_shared_persistence():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    root = Path(__file__).resolve().parents[2]
    process = subprocess.Popen(
        [sys.executable, str(root / "scripts/mobile_protocol_lab.py"), "--port", str(port)],
        cwd=root, env=dict(os.environ), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base_url = f"http://127.0.0.1:{port}"
        with httpx.Client(timeout=0.5, trust_env=False) as client:
            for _ in range(100):
                assert process.poll() is None, "disposable fixture exited before startup"
                try:
                    if client.get(base_url + "/fixture/native-credentials").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("disposable fixture failed to become ready")
        receipt = asyncio.run(verify_protocol(base_url))
        assert receipt["canonical_two_peers"] == "same runtime and persisted transcript"
        assert receipt["room_send_replay"] == "one durable event"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
