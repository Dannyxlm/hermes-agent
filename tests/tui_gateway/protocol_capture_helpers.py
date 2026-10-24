"""Stable request-frame capture projections for protocol tests."""
import json


def _not_shown(req):
    from tui_gateway import server_requests
    return {"jsonrpc": "2.0", "id": req.id, "error": {"code": server_requests.NOT_SHOWN_CODE, "message": "not here"}}


def _frames(buf):
    return [json.loads(line) for line in buf.getvalue().splitlines()]


def _request_frame_without_clock(frame):
    # server_time is sampled on every replay; all other wire fields must match.
    return {**frame, "params": {k: v for k, v in frame.get("params", {}).items() if k != "server_time"}}
