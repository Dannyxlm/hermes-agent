"""``GET /api/sessions/{id}/messages`` honors ``inline_images=false`` (#116511).

The REST pages carry ``content`` verbatim (a parts list with inline ``data:image`` URIs) so
Desktop's ``extractEmbeddedImages`` can pull them out of the text. A client that reads over
a network had no way to ask for less: the measured conversation was 26.33 MiB per page read.
``inline_images=false`` (default ``true``) routes the content through the same
``_coerce_message_text(image_urls=False)`` projection ``session.resume`` uses, rendering
``[image]`` in place of the data URI.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

DATA_URI = "data:image/png;base64," + "a" * 128
IMAGE_CONTENT = [
    {"type": "text", "text": "what is this?"},
    {"type": "image_url", "image_url": {"url": DATA_URI}},
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    from hermes_state import SessionDB

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", home / "state.db")

    db = SessionDB(home / "state.db")
    try:
        db.create_session(session_id="img-chat", source="desktop")
        db.append_messages_batch("img-chat", [
            {"role": "user", "content": IMAGE_CONTENT},
            {"role": "assistant", "content": "a chart"},
        ])
    finally:
        db.close()

    from hermes_cli.web_routers.sessions import manage_router

    app = FastAPI()
    app.include_router(manage_router)
    with TestClient(app) as test_client:
        yield test_client


def test_messages_default_inlines_images(client):
    page = client.get("/api/sessions/img-chat/messages?limit=10&order=oldest").json()
    user_row = next(m for m in page["messages"] if m["role"] == "user")
    assert DATA_URI in str(user_row["content"])


def test_messages_inline_images_false_renders_placeholder(client):
    page = client.get(
        "/api/sessions/img-chat/messages?limit=10&order=oldest&inline_images=false").json()
    user_row = next(m for m in page["messages"] if m["role"] == "user")
    assert "[image]" in user_row["content"]
    assert DATA_URI not in user_row["content"]
    # Non-image rows are untouched.
    assistant_row = next(m for m in page["messages"] if m["role"] == "assistant")
    assert assistant_row["content"] == "a chart"


def test_display_only_omits_replay_blobs_but_preserves_visible_and_raw_history(client):
    from hermes_state import SessionDB
    import json
    import os
    with SessionDB(Path(os.environ["HERMES_HOME"]) / "state.db") as db:
        db.append_messages_batch("img-chat", [{"role": "assistant", "content": "visible answer",
            "api_content": "MODEL-ONLY-" + "x" * 1024,
            "codex_reasoning_items": [{"encrypted_content": "OPAQUE-" + "x" * 1024}],
            "codex_message_items": [{"id": "MODEL-ONLY-ID"}],
            "reasoning_details": [{"type": "reasoning.text", "text": "visible reasoning", "signature": "OPAQUE-SIGNATURE"},
                                  {"type": "reasoning.text", "text": '{"example": "keep this prose"}'}],
            "display_metadata": {"tool_result_metadata": {"inline_diff": "+kept"}}}])
    url = "/api/sessions/img-chat/messages?limit=10&order=oldest&inline_images=false"
    display = client.get(url + "&display_only=true").json()["messages"][-1]
    assert display["content"] == "visible answer"
    assert display["display_metadata"]["tool_result_metadata"]["inline_diff"] == "+kept"
    assert "visible reasoning" in json.dumps(display["reasoning_details"])
    assert display["reasoning_details"][1]["text"] == '{"example": "keep this prose"}'
    assert "api_content" not in display and "codex_reasoning_items" not in display and "codex_message_items" not in display
    assert "OPAQUE" not in json.dumps(display) and "MODEL-ONLY" not in json.dumps(display)
    raw = client.get(url).json()["messages"][-1]
    assert raw["api_content"].startswith("MODEL-ONLY-")
    assert "OPAQUE-SIGNATURE" in raw["reasoning_details"]
