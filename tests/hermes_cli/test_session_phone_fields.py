"""Only phone-shaped display pages omit storage fields neither transcript decoder consumes."""
import os
from pathlib import Path

import pytest

from hermes_state import SessionDB
from tests.hermes_cli.test_session_messages_inline_images import client as client  # noqa: PLC0414 - pytest fixture


UNUSED_FIELDS = {"message_uid", "platform_message_id", "effect_disposition", "finish_reason", "token_count"}


@pytest.mark.parametrize("display_only,inline_images", [(True, False), (False, False), (True, True)])
def test_phone_shape_omits_only_unused_storage_members(client, display_only, inline_images):
    path = Path(os.environ["HERMES_HOME"]) / "state.db"
    with SessionDB(path) as db:
        db.append_messages_batch("img-chat", [{"role": "assistant", "content": "visible fixture answer",
            "reasoning_content": "visible fixture reasoning", "finish_reason": "stop", "token_count": 17,
            "display_metadata": {"tool_result_metadata": {"inline_diff": "+fixture"}}}])
    url = "/api/sessions/img-chat/messages?limit=100&order=latest&include_compacted=true"
    shape = f"&display_only={str(display_only).lower()}&inline_images={str(inline_images).lower()}"
    page = client.get(url + shape)
    assert page.status_code == 200
    row = page.json()["messages"][-1]
    control = client.get(url + "&display_only=true&inline_images=true").json()["messages"][-1]
    assert UNUSED_FIELDS <= control.keys(), "the control must carry the storage fields"
    if display_only and not inline_images:
        assert UNUSED_FIELDS.isdisjoint(row), "phone display pages must not serialize unused storage members"
        assert row == {key: value for key, value in control.items() if key not in UNUSED_FIELDS}
    else:
        assert UNUSED_FIELDS <= row.keys(), "Desktop/inspection request shapes must remain unchanged"
    assert row["content"] == "visible fixture answer"
    assert row["display_metadata"]["tool_result_metadata"]["inline_diff"] == "+fixture"
    assert row["id"] == control["id"]
    # Display projection must not alter durable rows or replay/export responses.
    raw = client.get(url).json()["messages"][-1]
    assert raw["message_uid"] == control["message_uid"]
    assert raw["finish_reason"] == "stop"
