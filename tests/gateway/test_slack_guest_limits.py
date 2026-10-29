"""Guest boundary regressions: prompt disclosure, upload parsing and per-sender cost."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from slack_sdk.web.async_client import AsyncWebClient

from agent.i18n import t
from gateway.config import HomeChannel, Platform, PlatformConfig
from gateway.guest_channels import guest_channel_authorized, guest_turn_toolsets
from gateway.platforms.event import MessageEvent
from gateway.session import build_session_context_prompt
from tests.waiting import async_wait_until
from tests.fakes.platforms.slack_standin import SlackStandin
from tests.gateway.test_slack_guest_channels import CHANNEL, GUEST, OWNER, TEAM, make_desk, source


@pytest.fixture
def desk(tmp_path, monkeypatch):
    return make_desk(tmp_path, monkeypatch)


def test_handbuilt_sources_do_not_require_slack_fields(desk):
    runner, adapter, _ = desk
    assert guest_turn_toolsets(runner, SimpleNamespace(), {}) is None
    src = SimpleNamespace(platform=Platform.SLACK, chat_type="group", chat_id=CHANNEL, scope_id=TEAM,
                          _transport_adapter_ref=lambda: adapter)
    assert guest_channel_authorized(runner, src) is False
    src.user_id = GUEST
    assert guest_channel_authorized(runner, src) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("user,restricted", [(GUEST, True), (OWNER, False)])
async def test_real_turn_preparation_and_pinned_context_hide_owner_destinations(desk, monkeypatch, user, restricted):
    runner, _, _ = desk
    runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(enabled=True, token="fake")
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM, chat_id="PRIVATE_CHAT", name="Owner destination")
    src = source(desk, user=user)
    entry = SimpleNamespace(yolo=None, session_key="guest-context", session_id="context-probe",
                            created_at=datetime.now(), updated_at=datetime.now())
    monkeypatch.setattr(runner, "_hmwa_open_session", AsyncMock(return_value=(False, False)))
    contexts = []

    class CapturedContext(Exception):
        pass

    def capture(context):
        contexts.append(context)
        raise CapturedContext

    monkeypatch.setattr(runner, "_set_session_env", capture)
    with pytest.raises(CapturedContext):
        await runner._hmwa_prepare_turn(MessageEvent(text="help", source=src), src, entry,
                                       "guest-context", "guest-context", 1)
    context = contexts[0]
    prompt = runner._pinned_session_context_prompt(context, False, None)
    assert "Current Session Context" in prompt and "Slack" in prompt
    for detail in ("Connected Platforms", "Home Channels", "scheduled tasks", "PRIVATE_CHAT", "cron/output"):
        assert (detail not in prompt) is restricted
    if restricted:
        assert context.connected_platforms == [] and context.home_channels == {}
    assert prompt == build_session_context_prompt(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["status", "context", "help"])
async def test_busy_guest_denial_is_localized_and_overrideable(desk, monkeypatch, command):
    runner, adapter, _ = desk
    monkeypatch.setattr(runner, "_handle_status_command", AsyncMock(side_effect=AssertionError("status leaked")))
    monkeypatch.setattr(runner, "_handle_context_command", AsyncMock(side_effect=AssertionError("context leaked")))
    src = source(desk)
    event = MessageEvent(text=f"/{command}", source=src)
    assert await runner._hm_busy_slash_or_photo(event, src, "guest") == (True, t("gateway.guest.slash_denied"))
    assert runner._check_slash_access(src, command) == t("gateway.guest.slash_denied")
    assert "gateway.guest.slash_denied" not in runner._check_slash_access(src, command)
    adapter.config.extra["guest_channels"][f"{TEAM}:{CHANNEL}"]["slash_denied"] = "Please use a message here."
    assert await runner._hm_busy_slash_or_photo(event, src, "guest") == (True, "Please use a message here.")
    assert runner._check_slash_access(source(desk, user=OWNER), command) is None


def test_missing_team_logs_and_denies_owner_too(desk, caplog):
    runner, _, _ = desk
    assert not runner._is_user_authorized_for_source(source(desk, user=OWNER, team=None))
    assert "guest-channel event lacks team_id" in caplog.text


def setup_slack(adapter, slack, capture):
    client = AsyncWebClient(token=slack.bot_token, base_url=slack.api_base)
    adapter._app = SimpleNamespace(client=client)
    adapter._team_clients[TEAM] = client
    adapter._bot_user_id = "UBOT0001"
    adapter._team_bot_user_ids[TEAM] = "UBOT0001"
    adapter.set_message_handler(capture)


def mention(user=GUEST, ts="100.001", **extra):
    return {"type": "app_mention", "user": user, "channel": CHANNEL, "channel_type": "channel",
            "team": TEAM, "text": "<@UBOT0001> Help with screenshot", "ts": ts, **extra}


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [None, 7])
async def test_cooldown_before_enrichment_is_per_user_across_threads_and_native_slash(desk, monkeypatch, seconds):
    _, adapter, _ = desk
    policy = adapter.config.extra["guest_channels"][f"{TEAM}:{CHANNEL}"]
    if seconds is not None:
        policy["min_seconds_between_turns"] = seconds
    interval = seconds or 20
    now = [100.0]
    monkeypatch.setattr("plugins.platforms.slack.adapter_guest.time", SimpleNamespace(monotonic=lambda: now[0]))
    capture = AsyncMock(return_value=None)
    hydrate = AsyncMock(return_value=(None, [], []))
    monkeypatch.setattr(adapter, "_hydrate_thread_context", hydrate)
    with SlackStandin() as slack:
        setup_slack(adapter, slack, capture)
        await adapter._handle_slack_message_impl(mention())
        await async_wait_until(lambda: capture.await_count == 1, what="first guest turn")
        await adapter._handle_slack_message_impl(mention(ts="101.001", thread_ts="99.001"))
        await adapter._handle_slash_command({"command": "/hermes", "text": "help with product",
                                            "user_id": GUEST, "channel_id": CHANNEL, "team_id": TEAM})
        assert capture.await_count == 1 and hydrate.await_count == 1
        await adapter._handle_slack_message_impl(mention(user="UOTHER", ts="102.001"))
        await adapter._handle_slack_message_impl(mention(user=OWNER, ts="103.001"))
        await adapter._handle_slack_message_impl(mention(user=OWNER, ts="104.001"))
        await async_wait_until(lambda: capture.await_count == 4, what="independent users and owner")
        assert capture.await_count == 4
        now[0] += interval
        await adapter._handle_slack_message_impl(mention(ts="105.001"))
        await async_wait_until(lambda: capture.await_count == 5, what="cooldown expiry")
        assert capture.await_count == 5
        assert slack.visible(CHANNEL) == []  # no cooldown response storm


def file(mime, size=12, name="upload"):
    return {"id": name, "name": name, "mimetype": mime, "size": size,
            "url_private_download": f"https://files.slack.com/{name}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("thread", [False, True])
@pytest.mark.parametrize("cap", [None, 20])
async def test_guest_media_images_only_in_trigger_and_thread_root(desk, monkeypatch, thread, cap):
    _, adapter, _ = desk
    if cap is not None:
        adapter.config.extra["guest_channels"][f"{TEAM}:{CHANNEL}"]["max_image_bytes"] = cap
    data = b"\x89PNG\r\n\x1a\n1234"
    download = AsyncMock(return_value=data)
    monkeypatch.setattr(adapter, "_download_slack_file_bytes", download)
    monkeypatch.setattr(adapter, "_cache_slack_document", AsyncMock(side_effect=AssertionError("document parsed")))
    monkeypatch.setattr(adapter, "_collect_thread_root_images", AsyncMock(side_effect=AssertionError("unbounded root")))
    capture = AsyncMock(return_value=None)
    files = [file("text/plain"), file("audio/mpeg"), file("video/mp4"), file("image/png", None),
             file("image/png", 5 * 1024 * 1024 + 1), file("image/png", len(data), "screen")]
    event = mention(files=[] if thread else files)
    if thread:
        event["thread_ts"] = "99.001"
        monkeypatch.setattr(adapter, "_has_active_session_for_thread", lambda **kw: False)
        adapter._thread_context_cache[adapter._thread_cache_key(CHANNEL, "99.001", TEAM)] = SimpleNamespace(
            messages=[{"ts": "99.001", "text": "root", "files": files}])
        monkeypatch.setattr(adapter, "_fetch_thread_context", AsyncMock(return_value="Root message"))
    with SlackStandin() as slack:
        setup_slack(adapter, slack, capture)
        await adapter._handle_slack_message_impl(event)
        await async_wait_until(lambda: capture.await_count == 1, what="image turn")
    received = capture.await_args.args[0]
    assert received.media_types == ["image/png"]
    assert received.media_text_inlined == [False]
    assert "Content of" not in received.text
    assert Path(received.media_urls[0]).read_bytes() == data
    assert download.await_count == 1
    assert download.await_args.kwargs["max_bytes"] == (cap or 5 * 1024 * 1024)


@pytest.mark.asyncio
async def test_configured_image_cap_and_actual_bytes_are_enforced(desk, monkeypatch):
    from plugins.platforms.slack.adapter_guest import collect_guest_media
    _, adapter, _ = desk
    download = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n" + b"x" * 100)
    monkeypatch.setattr(adapter, "_download_slack_file_bytes", download)
    event = mention(files=[file("image/png", 21), file("image/png", 10)])
    result = await collect_guest_media(adapter, event, CHANNEL, TEAM, "question", {"max_image_bytes": 20})
    assert result == ([], [], [], "question")
    assert download.await_count == 1 and download.await_args.kwargs["max_bytes"] == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("header,body,mime", [("100", b"x", "image/png"),
                                               (None, b"x" * 100, "image/png"),
                                               ("10", b"text data", "text/plain")])
async def test_guest_download_stream_stops_at_real_byte_limit(desk, monkeypatch, header, body, mime):
    import tools.url_safety as safety
    _, adapter, _ = desk
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield body
            raise AssertionError("oversized response was consumed after limit")

        async def aclose(self):
            closed.append(True)

    def respond(request):
        headers = {"content-type": mime}
        if header is not None:
            headers["content-length"] = header
        return httpx.Response(200, headers=headers, stream=Stream())

    monkeypatch.setattr(safety, "is_safe_url", lambda _: True)
    monkeypatch.setattr(safety, "create_ssrf_safe_async_client", lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(adapter, "_resolve_download_token", lambda *a: "offline")
    with pytest.raises(ValueError):
        await adapter._download_slack_file_bytes("https://files.slack.com/image", max_bytes=20)
    assert closed
