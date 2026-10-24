"""An accepted notification keeps its display identity on the real event wire."""
import threading
from types import SimpleNamespace

from tui_gateway import server
from tests.tui_gateway.test_auto_continue import _session

pytest_plugins = ["tests.tui_gateway.test_auto_continue"]


def test_accepted_notification_stream_has_one_presented_header(turn_env, marker_home, monkeypatch):
    frames = []
    monkeypatch.setattr(server, "write_json", lambda frame: frames.append(frame) or True)
    monkeypatch.setattr(server, "_start_usage_ticker", lambda *a: (
        threading.Event(), SimpleNamespace(join=lambda: None)))
    agent = SimpleNamespace(
        session_id="session-key", clear_interrupt=lambda: None,
        run_conversation=lambda *a, **k: {"final_response": "done", "messages": []},
    )
    session = _session(agent=agent, running=True)
    assert server._run_prompt_submit(
        "request", "session", session, "private model input",
        display_kind="process_complete_notice",
        display_metadata={"display_text": "Build completed", "process_ids": ["p1"],
                          "completed_count": 1},
    )
    starts = [f["params"]["payload"] for f in frames
              if f.get("params", {}).get("type") == "message.start"]
    assert starts == [{"presentation": {
        "display_kind": "process_complete_notice", "display_text": "Build completed",
        "process_ids": ["p1"], "completed_count": 1,
    }}]
