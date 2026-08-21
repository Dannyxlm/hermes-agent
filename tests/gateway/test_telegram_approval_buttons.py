"""Tests for Telegram inline keyboard approval buttons."""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import Platform, PlatformConfig


def _make_adapter(extra=None):
    """Create a TelegramAdapter with mocked internals."""
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


class _AuthRunner:
    """Minimal runner shim for callback auth tests."""

    def __init__(self, authorized: bool):
        self.authorized = authorized
        self.last_source = None

    async def _handle_message(self, event):
        return None

    def _is_user_authorized(self, source):
        self.last_source = source
        return self.authorized


# ===========================================================================
# send_exec_approval — inline keyboard buttons
# ===========================================================================

class TestTelegramExecApproval:
    """Test the send_exec_approval method sends InlineKeyboard buttons."""

    @pytest.mark.asyncio
    async def test_sends_inline_keyboard(self):
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 42
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        result = await adapter.send_exec_approval(
            chat_id="12345",
            command="rm -rf /important",
            session_key="agent:main:telegram:group:12345:99",
            description="dangerous deletion",
        )

        assert result.success is True
        assert result.message_id == "42"

        adapter._bot.send_message.assert_called_once()
        kwargs = adapter._bot.send_message.call_args[1]
        assert kwargs["chat_id"] == 12345
        assert "rm -rf /important" in kwargs["text"]
        assert "dangerous deletion" in kwargs["text"]
        assert kwargs["reply_markup"] is not None  # InlineKeyboardMarkup


    @pytest.mark.asyncio
    async def test_non_smart_allow_permanent_false_keeps_session(self, monkeypatch):
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=42))
        buttons = []
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardButton",
            lambda text, callback_data: buttons.append(text) or text,
        )
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardMarkup", lambda rows: rows
        )

        await adapter.send_exec_approval(
            chat_id="12345", command="curl example.test", session_key="s",
            allow_permanent=False,
        )

        assert buttons == ["✅ Allow Once", "✅ Session", "❌ Deny"]

    @pytest.mark.asyncio
    async def test_full_approval_keyboard_is_two_by_two(self, monkeypatch):
        """Regression: d48bf743f flattened all buttons into one row (4x1)."""
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=42))
        captured_rows = []
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardButton",
            lambda text, callback_data: text,
        )
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardMarkup",
            lambda rows: captured_rows.extend(rows) or rows,
        )

        await adapter.send_exec_approval(
            chat_id="12345", command="curl example.test", session_key="s",
        )

        assert captured_rows == [
            ["✅ Allow Once", "✅ Session"],
            ["✅ Always", "❌ Deny"],
        ]


    @pytest.mark.asyncio
    async def test_smart_deny_two_buttons_share_one_row(self, monkeypatch):
        """smart_deny yields 2 buttons — they pair into a single readable row."""
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=42))
        captured_rows = []
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardButton",
            lambda text, callback_data: text,
        )
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardMarkup",
            lambda rows: captured_rows.extend(rows) or rows,
        )

        await adapter.send_exec_approval(
            chat_id="12345", command="curl example.test", session_key="s",
            allow_permanent=False, smart_denied=True,
        )

        assert captured_rows == [
            ["✅ Allow Once", "❌ Deny"],
        ]


    @pytest.mark.asyncio
    async def test_send_update_prompt_escapes_dynamic_prompt(self):
        adapter = _make_adapter()
        sent = {}

        async def mock_send_message(**kwargs):
            sent.update(kwargs)
            return SimpleNamespace(message_id=55)

        adapter._bot.send_message = AsyncMock(side_effect=mock_send_message)

        result = await adapter.send_update_prompt(
            chat_id="12345",
            prompt="Fix [issue]_1 and verify *markdown*",
            default="alpha_beta",
            metadata={"thread_id": "999"},
        )

        assert result.success is True
        assert "MARKDOWN_V2" in repr(sent["parse_mode"])
        assert "Fix \\[issue\\]\\_1" in sent["text"]
        assert "alpha\\_beta" in sent["text"]

# _handle_callback_query — approval button clicks
# ===========================================================================

class TestTelegramApprovalCallback:
    """Test the approval callback handling in _handle_callback_query."""


    @pytest.mark.asyncio
    async def test_resume_typing_after_inline_approval(self):
        """Clicking an inline approval button must un-pause the chat's typing.

        Regression for #27853: the text /approve path resumed typing, but the
        ea: callback path did not, so the typing indicator stayed gone for the
        rest of a long-running turn after a button click.
        """
        adapter = _make_adapter()
        adapter._approval_state[5] = "agent:main:telegram:group:12345:99"
        adapter.pause_typing_for_chat("12345")
        assert "12345" in adapter._typing_paused

        query = AsyncMock()
        query.data = "ea:once:5"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.first_name = "Norbert"
        query.from_user.id = "12345"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            with patch("tools.approval.resolve_gateway_approval", return_value=1):
                await adapter._handle_callback_query(update, context)

        assert "12345" not in adapter._typing_paused


    @pytest.mark.asyncio
    async def test_approval_callback_escapes_dynamic_user_name(self):
        adapter = _make_adapter()
        adapter._approval_state[3] = "agent:main:telegram:group:12345:99"

        query = AsyncMock()
        query.data = "ea:once:3"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.first_name = "Alice_Bob"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()
        query.from_user.id = "12345"

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            with patch("tools.approval.resolve_gateway_approval", return_value=1):
                await adapter._handle_callback_query(update, context)

        edit_kwargs = query.edit_message_text.call_args[1]
        assert "MARKDOWN_V2" in repr(edit_kwargs["parse_mode"])
        assert "Alice\\_Bob" in edit_kwargs["text"]
        assert "Approved once" in edit_kwargs["text"]


    @pytest.mark.asyncio
    async def test_update_prompt_callback_not_affected(self, tmp_path):
        """Ensure update prompt callbacks still work."""
        adapter = _make_adapter()

        query = AsyncMock()
        query.data = "update_prompt:y"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.id = 123
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
            with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
                # Allow the caller — the new fail-closed allowlist gate
                # (#24457) rejects empty TELEGRAM_ALLOWED_USERS, but this
                # test isn't exercising that gate; it's verifying the
                # update_prompt callback still writes the response.
                with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}):
                    await adapter._handle_callback_query(update, context)

        # Should NOT have triggered approval resolution
        mock_resolve.assert_not_called()
        assert (tmp_path / ".update_response").read_text() == "y"

    @pytest.mark.asyncio
    async def test_update_prompt_callback_rejects_unauthorized_user(self, tmp_path):
        """Update prompt buttons should honor TELEGRAM_ALLOWED_USERS."""
        adapter = _make_adapter()

        query = AsyncMock()
        query.data = "update_prompt:y"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.id = 222
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
            with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "111"}):
                await adapter._handle_callback_query(update, context)

        query.answer.assert_called_once()
        assert "not authorized" in query.answer.call_args[1]["text"].lower()
        query.edit_message_text.assert_not_called()
        assert not (tmp_path / ".update_response").exists()

    @pytest.mark.asyncio
    async def test_update_prompt_callback_rejects_user_blocked_by_global_allowlist(self, tmp_path):
        adapter = _make_adapter()
        runner = _AuthRunner(authorized=False)
        adapter._message_handler = runner._handle_message

        query = AsyncMock()
        query.data = "update_prompt:y"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.chat.type = "private"
        query.from_user = MagicMock()
        query.from_user.id = 222
        query.from_user.first_name = "Mallory"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
            with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": ""}):
                await adapter._handle_callback_query(update, context)

        query.answer.assert_called_once()
        assert "not authorized" in query.answer.call_args[1]["text"].lower()
        query.edit_message_text.assert_not_called()
        assert not (tmp_path / ".update_response").exists()
        assert runner.last_source is not None
        assert runner.last_source.platform == Platform.TELEGRAM
        assert runner.last_source.user_id == "222"


class TestTelegramSlackApprovalCockpit:
    """CloudSeed Slack approval cards must dispatch locally and visibly."""

    @staticmethod
    def _query(data: str):
        query = MagicMock()
        query.data = data
        query.message = MagicMock()
        query.message.chat_id = -1003963755551
        query.message.chat.type = "supergroup"
        query.message.message_thread_id = 3252
        query.message.text = "Slack draft card"
        query.message.reply_markup = "keyboard"
        query.from_user = MagicMock()
        query.from_user.id = 123
        query.from_user.first_name = "Danny"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        return query

    def test_wrapper_path_requires_executable_file(self, tmp_path):
        adapter = _make_adapter()
        wrapper = tmp_path / "scripts" / "slack-approval" / "slack-approval.sh"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("#!/bin/sh\n", encoding="utf-8")

        with patch("hermes_constants.get_hermes_home", return_value=tmp_path), patch(
            "plugins.platforms.telegram.adapter.os.access",
            side_effect=[False, True],
        ):
            assert adapter._available_slack_approval_wrapper_path() is None
            assert adapter._available_slack_approval_wrapper_path() == wrapper

    @pytest.mark.asyncio
    async def test_skip_callback_runs_bounded_action_and_closes_keyboard(self):
        adapter = _make_adapter()
        adapter._is_callback_user_authorized = lambda *args, **kwargs: True
        adapter._run_slack_approval_action = AsyncMock(
            return_value=(True, "⏭ Skipped s288e675f7b.")
        )
        query = self._query("sa:skip:s288e675f7b")

        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), SimpleNamespace()
        )

        query.answer.assert_awaited_once_with(text="Working on Slack skip…")
        adapter._run_slack_approval_action.assert_awaited_once_with(
            "skip", "s288e675f7b", actor_id="123"
        )
        edit_kwargs = query.edit_message_text.call_args.kwargs
        assert "Skipped s288e675f7b" in edit_kwargs["text"]
        assert edit_kwargs["reply_markup"] is None

    @pytest.mark.asyncio
    async def test_callback_preserves_receipt_when_card_nears_telegram_limit(self):
        adapter = _make_adapter()
        adapter._is_callback_user_authorized = lambda *args, **kwargs: True
        adapter._run_slack_approval_action = AsyncMock(
            return_value=(True, "✅ Sent s288e675f7b to Slack.")
        )
        query = self._query("sa:send:s288e675f7b")
        query.message.text = "x" * 4090

        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), SimpleNamespace()
        )

        text = query.edit_message_text.call_args.kwargs["text"]
        assert len(text) <= 4096
        assert text.endswith("✅ Sent s288e675f7b to Slack.")

    @pytest.mark.asyncio
    async def test_missing_wrapper_callback_keeps_card_actionable(self):
        adapter = _make_adapter()
        adapter._is_callback_user_authorized = lambda *args, **kwargs: True
        adapter._available_slack_approval_wrapper_path = MagicMock(return_value=None)
        query = self._query("sa:send:s288e675f7b")

        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), SimpleNamespace()
        )

        query.answer.assert_awaited_once_with(text="Working on Slack send…")
        edit_kwargs = query.edit_message_text.call_args.kwargs
        assert edit_kwargs["text"].endswith("❌ Slack approval wrapper is unavailable.")
        assert edit_kwargs["reply_markup"] == "keyboard"

    @pytest.mark.asyncio
    async def test_callback_rejects_unauthorized_user_without_running_action(self):
        adapter = _make_adapter()
        adapter._is_callback_user_authorized = lambda *args, **kwargs: False
        adapter._run_slack_approval_action = AsyncMock()
        query = self._query("sa:send:s288e675f7b")

        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), SimpleNamespace()
        )

        assert "not authorized" in query.answer.call_args.kwargs["text"].lower()
        adapter._run_slack_approval_action.assert_not_awaited()
        query.edit_message_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_edit_command_preserves_message_as_one_subprocess_argument(self):
        adapter = _make_adapter()
        adapter._is_callback_user_authorized = lambda *args, **kwargs: True
        adapter._available_slack_approval_wrapper_path = MagicMock(
            return_value=Path("slack-approval.sh")
        )
        adapter._run_slack_approval_action = AsyncMock(
            return_value=(True, "✏️ Updated s288e675f7b.")
        )
        msg = MagicMock()
        msg.text = "/sedit s288e675f7b keep this exact Slack message"
        msg.from_user.id = 123
        msg.reply_text = AsyncMock()

        handled = await adapter._handle_slack_approval_command_message(msg)

        assert handled is True
        adapter._run_slack_approval_action.assert_awaited_once_with(
            "edit",
            "s288e675f7b",
            actor_id="123",
            edit_text="keep this exact Slack message",
        )
        msg.reply_text.assert_awaited_once_with("✏️ Updated s288e675f7b.")

    @pytest.mark.asyncio
    async def test_edit_action_rejects_option_shaped_body_before_subprocess(self):
        adapter = _make_adapter()

        success, result = await adapter._run_slack_approval_action(
            "edit",
            "s288e675f7b",
            actor_id="123",
            edit_text="--dry-run should stay message text",
        )

        assert success is False
        assert "cannot begin with a dash" in result

    @pytest.mark.asyncio
    async def test_missing_wrapper_action_returns_explicit_error(self):
        adapter = _make_adapter()
        adapter._available_slack_approval_wrapper_path = MagicMock(return_value=None)

        success, result = await adapter._run_slack_approval_action(
            "send",
            "s288e675f7b",
            actor_id="123",
        )

        assert success is False
        assert result == "❌ Slack approval wrapper is unavailable."

    @pytest.mark.asyncio
    async def test_same_draft_actions_are_serialized(self):
        adapter = _make_adapter()
        active = 0
        max_active = 0

        async def fake_unlocked(*args, **kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return True, "ok"

        adapter._run_slack_approval_action_unlocked = fake_unlocked
        await asyncio.gather(
            adapter._run_slack_approval_action(
                "send", "s288e675f7b", actor_id="123"
            ),
            adapter._run_slack_approval_action(
                "send", "s288e675f7b", actor_id="123"
            ),
        )

        assert max_active == 1

    @pytest.mark.asyncio
    async def test_command_strictly_rechecks_runner_authorization(self):
        adapter = _make_adapter()
        runner = _AuthRunner(authorized=False)
        adapter._message_handler = runner._handle_message
        adapter._available_slack_approval_wrapper_path = MagicMock(
            return_value=Path("slack-approval.sh")
        )
        adapter._run_slack_approval_action = AsyncMock()
        msg = MagicMock()
        msg.text = "/ssend s288e675f7b"
        msg.from_user.id = 222
        msg.from_user.username = "blocked"
        msg.chat.id = -1003963755551
        msg.chat.type = "supergroup"
        msg.chat.is_forum = True
        msg.message_thread_id = 3252
        msg.is_topic_message = True
        msg.reply_text = AsyncMock()

        with patch.dict(os.environ, {}, clear=True):
            handled = await adapter._handle_slack_approval_command_message(msg)

        assert handled is True
        assert "not authorized" in msg.reply_text.call_args.args[0].lower()
        adapter._run_slack_approval_action.assert_not_awaited()
        assert runner.last_source is not None
        assert runner.last_source.user_id == "222"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "/ssend s288e675f7b",
            "/sedit s288e675f7b revised message",
            "/sskip s288e675f7b",
            "/stask s288e675f7b",
            "/sview s288e675f7b",
        ],
    )
    async def test_commands_fall_through_when_wrapper_is_unavailable(self, command):
        adapter = _make_adapter()
        adapter._available_slack_approval_wrapper_path = MagicMock(return_value=None)
        adapter._is_callback_user_authorized = MagicMock(return_value=True)
        adapter._run_slack_approval_action = AsyncMock()
        msg = MagicMock()
        msg.text = command
        msg.reply_text = AsyncMock()

        handled = await adapter._handle_slack_approval_command_message(msg)

        assert handled is False
        adapter._is_callback_user_authorized.assert_not_called()
        adapter._run_slack_approval_action.assert_not_awaited()
        msg.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_command_addressed_to_another_bot_is_not_intercepted(self):
        adapter = _make_adapter()
        adapter._bot.username = "AvaBot"
        msg = MagicMock()
        msg.text = "/ssend@OtherBot s288e675f7b"

        assert await adapter._handle_slack_approval_command_message(msg) is False

    @pytest.mark.asyncio
    async def test_unrelated_command_is_not_intercepted(self):
        adapter = _make_adapter()
        msg = MagicMock()
        msg.text = "/status"

        assert await adapter._handle_slack_approval_command_message(msg) is False
