"""Guest intake and prompt boundaries preserve owner and warm-thread behavior."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent import i18n
from gateway.config import ChannelOverride, Platform
from gateway.guest_channels import restrict_guest_context
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext
from tests.fakes.platforms.slack_standin import SlackStandin
from tests.gateway.test_slack_guest_channels import CHANNEL, GUEST, OWNER, TEAM, make_desk, source
from tests.gateway.test_slack_guest_limits import file, mention, setup_slack
from tests.waiting import async_wait_until


@pytest.fixture
def desk(tmp_path, monkeypatch):
    return make_desk(tmp_path, monkeypatch)


@pytest.mark.parametrize("lang", [lang for lang in i18n.SUPPORTED_LANGUAGES if lang != "en"])
def test_guest_slash_denial_is_translated_at_delivery(desk, monkeypatch, lang):
    runner, _, home = desk
    (home.parent.parent / "config.yaml").write_text(f"display:\n  language: {lang}\n")
    i18n.reset_language_cache()
    denial = runner._check_slash_access(source(desk), "help")
    assert denial == i18n.t("gateway.guest.slash_denied", lang=lang)
    assert denial != i18n.t("gateway.guest.slash_denied", lang="en")
    assert denial and "gateway.guest.slash_denied" not in denial


@pytest.mark.parametrize("context", [None, object(), SimpleNamespace(), SimpleNamespace(source=None),
                                     SimpleNamespace(source=object())])
def test_context_without_slack_source_is_preserved(desk, context):
    runner, _, _ = desk
    assert restrict_guest_context(runner, context) is context


@pytest.mark.parametrize("user,restricted", [(GUEST, True), (OWNER, False)])
def test_guest_ephemeral_prompt_excludes_receiving_bot_channel_prompts(desk, user, restricted):
    runner, _, _ = desk
    runner.config.platforms[Platform.SLACK].channel_overrides[CHANNEL] = ChannelOverride(
        system_prompt="Private channel override")
    ctx = TurnContext(source=source(desk, user=user), context_prompt="Safe session context",
                      channel_prompt="Private receiving bot hint")
    prompt = TurnRunner(runner, ctx)._combined_ephemeral_prompt()
    assert "Safe session context" in prompt
    for private in ("Private receiving bot hint", "Private channel override"):
        assert (private not in prompt) is restricted


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_thread_root_image_is_only_downloaded_on_cold_start(desk, monkeypatch, active):
    _, adapter, _ = desk
    root = file("image/png", 12, "root")
    trigger = file("image/png", 12, "trigger")
    monkeypatch.setattr(adapter, "_has_active_session_for_thread", lambda **kw: active)
    monkeypatch.setattr(adapter, "_fetch_thread_context", AsyncMock(return_value="Thread context"))
    adapter._thread_context_cache[adapter._thread_cache_key(CHANNEL, "99.001", TEAM)] = SimpleNamespace(
        messages=[{"ts": "99.001", "files": [root]}])
    download = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n1234")
    monkeypatch.setattr(adapter, "_download_slack_file_bytes", download)
    capture = AsyncMock(return_value=None)
    with SlackStandin() as slack:
        setup_slack(adapter, slack, capture)
        await adapter._handle_slack_message_impl(mention(thread_ts="99.001", files=[trigger]))
        await async_wait_until(lambda: capture.await_count == 1, what="guest thread image turn")
    urls = [call.args[0] for call in download.await_args_list]
    assert urls == ([trigger["url_private_download"]] if active else
                    [root["url_private_download"], trigger["url_private_download"]])
    assert capture.await_args.args[0].media_types == ["image/png"] * len(urls)


@pytest.mark.asyncio
async def test_each_dropped_attachment_has_one_neutral_note_in_delivered_text(desk, monkeypatch):
    _, adapter, _ = desk
    adapter.config.extra["guest_channels"][f"{TEAM}:{CHANNEL}"]["max_image_bytes"] = 12
    files = [file("application/pdf"), file("image/png", None), file("image/png", 13),
             file("image/png", 12, "broken"), file("image/png", 12, "spoof"),
             file("image/png", 12, "screen"), file("image/png", 12, "over-budget")]
    download = AsyncMock(side_effect=[ValueError("bad response"), b"not an image", b"\x89PNG\r\n\x1a\n1234"])
    monkeypatch.setattr(adapter, "_download_slack_file_bytes", download)
    capture = AsyncMock(return_value=None)
    with SlackStandin() as slack:
        setup_slack(adapter, slack, capture)
        await adapter._handle_slack_message_impl(mention(files=files))
        await async_wait_until(lambda: capture.await_count == 1, what="guest dropped attachment notes")
    received = capture.await_args.args[0]
    notes = [line for line in received.text.splitlines() if line.startswith("[attachment not read:")]
    assert len(notes) == len(files) - len(received.media_urls) == 6
    assert len(set(notes)) == 1
    assert "only PNG/JPEG/GIF/WebP screenshots" in notes[0]
    assert "12 bytes" in notes[0]
    assert "Help with screenshot" in received.text
    assert received.media_types == ["image/png"]
    assert download.await_count == 3


def test_guest_admission_honors_managed_restrictions(desk, monkeypatch, tmp_path):
    runner, _, _ = desk
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    src = source(desk)
    assert runner._is_user_authorized_for_source(src)
    (managed / "config.yaml").write_text("platform_toolsets:\n  slack: [terminal]\n")
    assert not runner._is_user_authorized_for_source(src)


def test_guest_admission_never_recovers_a_corrupt_profile_config(desk):
    from hermes_cli.config_effective import load_user_config_effective
    runner, _, home = desk
    src = source(desk)
    config_path = home / "config.yaml"
    assert load_user_config_effective(config_path, fail_closed=True)["platform_toolsets"]["slack"] == []
    assert runner._is_user_authorized_for_source(src)
    config_path.write_text("memory: [")
    assert not runner._is_user_authorized_for_source(src)
