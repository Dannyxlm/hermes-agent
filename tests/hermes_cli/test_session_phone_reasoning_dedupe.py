"""Phone display pages drop the duplicate reasoning copies.

``display_only`` keeps ``reasoning``, ``reasoning_content`` and ``reasoning_details`` (all the same
text); the phone's transcript mapper reads ``reasoning`` first and falls back to the other two only
when it is blank. Desktop and inspection request shapes stay unchanged.
"""
import os
from pathlib import Path

import pytest

from hermes_state import SessionDB
from tests.hermes_cli.test_session_messages_inline_images import client as client  # noqa: PLC0414 - pytest fixture

URL = "/api/sessions/img-chat/messages?limit=100&order=latest&include_compacted=true&display_only=true"
DETAILS = [{"type": "reasoning.text", "text": "thought"}]


def _rows(client, inline_images):
    path = Path(os.environ["HERMES_HOME"]) / "state.db"
    with SessionDB(path) as db:
        db.append_messages_batch("img-chat", [
            {"role": "assistant", "content": "a", "reasoning": "thought", "reasoning_content": "thought",
             "reasoning_details": DETAILS},
            {"role": "assistant", "content": "b", "reasoning_content": "only the copy", "reasoning_details": DETAILS},
            {"role": "assistant", "content": "c", "reasoning": "  \n", "reasoning_content": "padded", },
        ])
    rows = client.get(URL + f"&inline_images={str(inline_images).lower()}").json()["messages"]
    return {row["content"]: row for row in rows if row["content"] in ("a", "b", "c")}


def test_duplicate_reasoning_is_dropped_from_phone_pages(client):
    rows = _rows(client, inline_images=False)
    assert rows["a"]["reasoning"] == "thought"
    assert "reasoning_content" not in rows["a"] and "reasoning_details" not in rows["a"]


def test_a_copy_is_kept_whenever_the_phone_would_fall_back_to_it(client):
    rows = _rows(client, inline_images=False)
    assert not rows["b"].get("reasoning") and rows["b"]["reasoning_content"] == "only the copy"
    assert rows["b"]["reasoning_details"]
    assert rows["c"]["reasoning_content"] == "padded"  # blank ``reasoning`` is not readable


def test_other_request_shapes_are_unchanged(client):
    rows = _rows(client, inline_images=True)
    assert {"reasoning", "reasoning_content", "reasoning_details"} <= rows["a"].keys()
    raw = client.get("/api/sessions/img-chat/messages?limit=100&order=latest").json()["messages"]
    assert {"reasoning", "reasoning_content", "reasoning_details"} <= next(
        row for row in raw if row["content"] == "a").keys()
