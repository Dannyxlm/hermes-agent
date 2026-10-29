"""Slack guest ingress and reply transport against the local Web API stand-in."""
from types import SimpleNamespace

import pytest
from slack_sdk.web.async_client import AsyncWebClient

from tests.fakes.platforms.slack_standin import SlackStandin
from tests.gateway import test_slack_guest_channels
from tests.gateway.test_slack_guest_channels import CHANNEL, GUEST, OWNER, TEAM
from tests.waiting import async_wait_until


@pytest.fixture
def desk(tmp_path, monkeypatch):
    return test_slack_guest_channels.make_desk(tmp_path, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("user,channel,kind,team,expected", [
    (GUEST, CHANNEL, "channel", TEAM, "guest-support"),
    (OWNER, CHANNEL, "channel", TEAM, "default"),
    (GUEST, "COTHER", "channel", TEAM, None),
    (GUEST, "DOTHER", "im", TEAM, None),
    (GUEST, CHANNEL, "channel", "TOTHER", None),
])
async def test_native_slack_event_preserves_scope_and_routes_before_one_thread_reply(
        desk, user, channel, kind, team, expected):
    runner, adapter, _ = desk
    with SlackStandin() as slack:
        client = AsyncWebClient(token=slack.bot_token, base_url=slack.api_base)
        adapter._app = SimpleNamespace(client=client)
        adapter._team_clients[team] = client
        adapter._bot_user_id = "UBOT0001"
        adapter._team_bot_user_ids[team] = "UBOT0001"
        received = []

        async def reply(event):
            assert runner._is_user_authorized_for_source(event.source)
            received.append(event)
            return "What steps reproduce it?"

        adapter.set_message_handler(reply)
        await adapter._handle_slack_message_impl({
            "type": "app_mention", "user": user, "channel": channel, "channel_type": kind,
            "team": team, "text": "<@UBOT0001> The product button is broken", "ts": "100.001",
        }, {"team_id": team})
        if expected is None:
            assert received == []
            assert slack.visible(channel) == []
            return
        replies = await async_wait_until(lambda: slack.visible(channel), what="one threaded reply")
        assert received[0].source.scope_id == team
        assert received[0].source.profile == expected
        assert len(received) == 1
        assert len(replies) == 1
        assert replies[0].extra["thread_ts"] == "100.001"


@pytest.mark.asyncio
async def test_owner_native_slash_keeps_workspace_scope_and_normal_profile(desk):
    runner, adapter, _ = desk
    received = []

    async def capture(event):
        assert runner._is_user_authorized_for_source(event.source)
        received.append(event)

    with SlackStandin() as slack:
        client = AsyncWebClient(token=slack.bot_token, base_url=slack.api_base)
        adapter._app = SimpleNamespace(client=client)
        adapter._team_clients[TEAM] = client
        adapter.set_message_handler(capture)
        await adapter._handle_slash_command({"command": "/whoami", "user_id": OWNER,
                                            "channel_id": CHANNEL, "team_id": TEAM, "text": ""})
        await async_wait_until(lambda: received, what="owner native slash handoff")
        assert received[0].source.scope_id == TEAM
        assert received[0].source.profile == "default"
