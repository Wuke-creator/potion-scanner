"""Unit tests for admin Telegram command handlers.

Tests /users, /extend, /revoke, /kill, /resume, /broadcast, and
the kill confirmation callback. Mocks user_db, orchestrator, and
Telegram Update/Context objects.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.state.user_db import UserRecord
from src.telegram.handlers.admin import (
    admin_callback,
    broadcast_command,
    extend_command,
    kill_command,
    resume_command,
    revoke_command,
    users_command,
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

ADMIN_ID = 99999


def _make_user(user_id="user-1", display_name="Alice", status="active"):
    return UserRecord(
        user_id=user_id,
        display_name=display_name,
        status=status,
        created_at="2025-01-01T00:00:00+00:00",
        updated_at="2025-01-01T00:00:00+00:00",
    )


def _make_context(args=None, admin=True, user_db=None, orchestrator=None):
    """Build a mock Context with bot_data containing user_db and orchestrator."""
    if user_db is None:
        user_db = MagicMock()
    if orchestrator is None:
        orchestrator = MagicMock()

    context = MagicMock()
    context.args = args or []
    context.bot_data = {
        "user_db": user_db,
        "orchestrator": orchestrator,
        "admin_ids": [ADMIN_ID] if admin else [],
    }
    context.bot = AsyncMock()
    return context


def _make_update(user_id=ADMIN_ID):
    """Build a mock Update for a command message."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    return update


def _make_callback_update(callback_data, user_id=ADMIN_ID):
    """Build a mock Update for an inline callback query."""
    query = AsyncMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = user_id
    return update


# ------------------------------------------------------------------
# /users
# ------------------------------------------------------------------

class TestUsersCommand:
    @pytest.mark.asyncio
    async def test_no_users(self):
        user_db = MagicMock()
        user_db.list_users.return_value = []
        context = _make_context(user_db=user_db)
        update = _make_update()

        await users_command(update, context)

        update.message.reply_text.assert_called_once_with("No users registered.")

    @pytest.mark.asyncio
    async def test_two_users_listed(self):
        u1 = _make_user("user-1", "Alice", "active")
        u2 = _make_user("user-2", "Bob", "inactive")
        user_db = MagicMock()
        user_db.list_users.return_value = [u1, u2]
        user_db.get_user_config.side_effect = [
            {"active_preset": "runner"},
            {"active_preset": "scalper"},
        ]
        user_db.get_access_expiry.side_effect = [
            "2025-06-01T00:00:00+00:00",
            None,
        ]
        context = _make_context(user_db=user_db)
        update = _make_update()

        await users_command(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "user-1" in text
        assert "user-2" in text
        assert "Alice" in text
        assert "Bob" in text
        assert "runner" in text
        assert "scalper" in text
        assert "2025-06-01" in text
        assert "unlimited" in text


# ------------------------------------------------------------------
# /extend
# ------------------------------------------------------------------

class TestExtendCommand:
    @pytest.mark.asyncio
    async def test_success(self):
        user_db = MagicMock()
        user_db.get_user.return_value = _make_user("user-1")
        user_db.extend_user_access.return_value = "2025-07-01T00:00:00+00:00"
        context = _make_context(args=["user-1", "30"], user_db=user_db)
        update = _make_update()

        await extend_command(update, context)

        user_db.extend_user_access.assert_called_once_with("user-1", 30)
        text = update.message.reply_text.call_args[0][0]
        assert "user-1" in text
        assert "30" in text

    @pytest.mark.asyncio
    async def test_missing_args(self):
        context = _make_context(args=[])
        update = _make_update()

        await extend_command(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_unknown_user(self):
        user_db = MagicMock()
        user_db.get_user.return_value = None
        context = _make_context(args=["unknown-user", "30"], user_db=user_db)
        update = _make_update()

        await extend_command(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "not found" in text


# ------------------------------------------------------------------
# /revoke
# ------------------------------------------------------------------

class TestRevokeCommand:
    @pytest.mark.asyncio
    async def test_success(self):
        user_db = MagicMock()
        user_db.get_user.return_value = _make_user("user-1")
        orchestrator = MagicMock()
        context = _make_context(args=["user-1"], user_db=user_db, orchestrator=orchestrator)
        update = _make_update()

        await revoke_command(update, context)

        user_db.revoke_user_access.assert_called_once_with("user-1")
        orchestrator.deactivate_user.assert_called_once_with("user-1")
        text = update.message.reply_text.call_args[0][0]
        assert "revoked" in text.lower()

    @pytest.mark.asyncio
    async def test_missing_args(self):
        context = _make_context(args=[])
        update = _make_update()

        await revoke_command(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text


# ------------------------------------------------------------------
# /kill
# ------------------------------------------------------------------

class TestKillCommand:
    @pytest.mark.asyncio
    async def test_shows_confirmation(self):
        context = _make_context()
        update = _make_update()

        await kill_command(update, context)

        call_kwargs = update.message.reply_text.call_args[1]
        assert "reply_markup" in call_kwargs
        markup = call_kwargs["reply_markup"]
        button_data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert "admin:kill_confirm" in button_data
        assert "admin:kill_cancel" in button_data


# ------------------------------------------------------------------
# Kill confirm/cancel callbacks
# ------------------------------------------------------------------

class TestAdminCallback:
    @pytest.mark.asyncio
    async def test_kill_confirm(self):
        orchestrator = MagicMock()
        orchestrator.kill_all.return_value = {
            "user-1": {"closed": 2, "errors": []},
        }
        context = _make_context(orchestrator=orchestrator)
        update = _make_callback_update("admin:kill_confirm")

        await admin_callback(update, context)

        orchestrator.kill_all.assert_called_once()
        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "Kill Switch Activated" in text
        assert "user-1" in text
        assert "2 closed" in text

    @pytest.mark.asyncio
    async def test_kill_cancel(self):
        context = _make_context()
        update = _make_callback_update("admin:kill_cancel")

        await admin_callback(update, context)

        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "canceled" in text.lower()


# ------------------------------------------------------------------
# /resume
# ------------------------------------------------------------------

class TestResumeCommand:
    @pytest.mark.asyncio
    async def test_resume(self):
        orchestrator = MagicMock()
        context = _make_context(orchestrator=orchestrator)
        update = _make_update()

        await resume_command(update, context)

        orchestrator.resume.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "resumed" in text.lower()


# ------------------------------------------------------------------
# /broadcast
# ------------------------------------------------------------------

class TestBroadcastCommand:
    @pytest.mark.asyncio
    async def test_success(self):
        user_db = MagicMock()
        user_db.get_all_telegram_chat_ids.return_value = [111, 222]
        context = _make_context(args=["Hello", "everyone!"], user_db=user_db)
        update = _make_update()

        await broadcast_command(update, context)

        assert context.bot.send_message.call_count == 2
        # Check the broadcast message content
        sent_text = context.bot.send_message.call_args_list[0][1]["text"]
        assert "Hello everyone!" in sent_text
        assert "Admin Broadcast" in sent_text
        # Check delivery summary
        reply_text = update.message.reply_text.call_args[0][0]
        assert "Delivered: 2" in reply_text
        assert "Failed: 0" in reply_text

    @pytest.mark.asyncio
    async def test_no_message(self):
        context = _make_context(args=[])
        update = _make_update()

        await broadcast_command(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_partial_failure(self):
        user_db = MagicMock()
        user_db.get_all_telegram_chat_ids.return_value = [111, 222]
        context = _make_context(args=["Test"], user_db=user_db)
        # First send succeeds, second fails
        context.bot.send_message = AsyncMock(side_effect=[None, Exception("blocked")])
        update = _make_update()

        await broadcast_command(update, context)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "Delivered: 1" in reply_text
        assert "Failed: 1" in reply_text


# ------------------------------------------------------------------
# Non-admin rejection
# ------------------------------------------------------------------

class TestNonAdminRejected:
    @pytest.mark.asyncio
    async def test_non_admin_users_command(self):
        context = _make_context(admin=False)
        update = _make_update(user_id=12345)  # Not in admin_ids

        await users_command(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "administrator" in text.lower()

    @pytest.mark.asyncio
    async def test_non_admin_callback_rejected(self):
        context = _make_context(admin=False)
        update = _make_callback_update("admin:kill_confirm", user_id=12345)

        await admin_callback(update, context)

        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "administrator" in text.lower()
