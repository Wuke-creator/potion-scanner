"""Unit tests for expiry enforcement — ExpiryChecker background task.

Tests:
- Expired user: pipeline deactivated, status set inactive, notification sent
- 3-day warning: notification sent, deduplicated on second check
- 1-day warning: notification sent, deduplicated on second check
- No warnings for non-expiring users
- /extend re-activates expired (inactive) user
- registered_only blocks expired users
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.state.user_db import UserRecord
from src.telegram.expiry_checker import ExpiryChecker
from src.telegram.handlers.admin import extend_command


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

ADMIN_ID = 99999


def _make_checker(
    expired_users=None,
    expiring_3d=None,
    expiring_1d=None,
    chat_ids=None,
):
    """Build an ExpiryChecker with mocked dependencies."""
    bot = AsyncMock()
    user_db = MagicMock()
    orchestrator = MagicMock()

    user_db.get_expired_users.return_value = expired_users or []
    user_db.get_users_expiring_within.side_effect = lambda hours: {
        72: expiring_3d or [],
        24: expiring_1d or [],
    }.get(hours, [])

    # Map user_id -> chat_id
    _chat_ids = chat_ids or {}
    user_db.get_telegram_chat_id.side_effect = lambda uid: _chat_ids.get(uid)

    checker = ExpiryChecker(
        bot=bot,
        user_db=user_db,
        orchestrator=orchestrator,
        interval_sec=60,
    )
    return checker, bot, user_db, orchestrator


def _make_context(args=None, user_db=None, orchestrator=None):
    if user_db is None:
        user_db = MagicMock()
    if orchestrator is None:
        orchestrator = MagicMock()

    context = MagicMock()
    context.args = args or []
    context.bot_data = {
        "user_db": user_db,
        "orchestrator": orchestrator,
        "admin_ids": [ADMIN_ID],
    }
    context.bot = AsyncMock()
    return context


def _make_update(user_id=ADMIN_ID):
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    return update


# ------------------------------------------------------------------
# ExpiryChecker — expired users
# ------------------------------------------------------------------

class TestExpiryCheckerExpired:
    @pytest.mark.asyncio
    async def test_expired_user_deactivated_and_notified(self):
        checker, bot, user_db, orchestrator = _make_checker(
            expired_users=["user-1"],
            chat_ids={"user-1": 111},
        )

        result = await checker.check_expiry()

        assert result["expired"] == ["user-1"]
        user_db.set_user_status.assert_called_once_with("user-1", "inactive")
        orchestrator.deactivate_user.assert_called_once_with("user-1")
        bot.send_message.assert_called_once()
        text = bot.send_message.call_args[1]["text"]
        assert "Access Expired" in text

    @pytest.mark.asyncio
    async def test_expired_user_no_chat_id(self):
        """Expired user without chat_id — deactivated but no notification sent."""
        checker, bot, user_db, orchestrator = _make_checker(
            expired_users=["user-1"],
            chat_ids={},  # no chat id
        )

        result = await checker.check_expiry()

        assert result["expired"] == ["user-1"]
        user_db.set_user_status.assert_called_once_with("user-1", "inactive")
        orchestrator.deactivate_user.assert_called_once_with("user-1")
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_expired_users(self):
        checker, bot, user_db, orchestrator = _make_checker(
            expired_users=["user-1", "user-2"],
            chat_ids={"user-1": 111, "user-2": 222},
        )

        result = await checker.check_expiry()

        assert result["expired"] == ["user-1", "user-2"]
        assert user_db.set_user_status.call_count == 2
        assert orchestrator.deactivate_user.call_count == 2
        assert bot.send_message.call_count == 2


# ------------------------------------------------------------------
# ExpiryChecker — warnings
# ------------------------------------------------------------------

class TestExpiryCheckerWarnings:
    @pytest.mark.asyncio
    async def test_3d_warning_sent(self):
        expiry = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        checker, bot, user_db, _ = _make_checker(
            expiring_3d=[("user-1", expiry)],
            chat_ids={"user-1": 111},
        )

        result = await checker.check_expiry()

        assert result["warned_3d"] == ["user-1"]
        text = bot.send_message.call_args[1]["text"]
        assert "Access Expiry Warning" in text

    @pytest.mark.asyncio
    async def test_3d_warning_deduplicated(self):
        expiry = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        checker, bot, user_db, _ = _make_checker(
            expiring_3d=[("user-1", expiry)],
            chat_ids={"user-1": 111},
        )

        await checker.check_expiry()
        bot.send_message.reset_mock()

        result = await checker.check_expiry()

        assert result["warned_3d"] == []
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_1d_warning_sent(self):
        expiry = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        checker, bot, user_db, _ = _make_checker(
            expiring_1d=[("user-1", expiry)],
            chat_ids={"user-1": 111},
        )

        result = await checker.check_expiry()

        assert result["warned_1d"] == ["user-1"]
        text = bot.send_message.call_args[1]["text"]
        assert "Urgent" in text

    @pytest.mark.asyncio
    async def test_1d_warning_deduplicated(self):
        expiry = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        checker, bot, user_db, _ = _make_checker(
            expiring_1d=[("user-1", expiry)],
            chat_ids={"user-1": 111},
        )

        await checker.check_expiry()
        bot.send_message.reset_mock()

        result = await checker.check_expiry()

        assert result["warned_1d"] == []
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_warnings_cleared_after_expiry(self):
        """After a user expires, warnings are cleared so they fire again if re-activated."""
        expiry = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        checker, bot, user_db, _ = _make_checker(
            expiring_1d=[("user-1", expiry)],
            chat_ids={"user-1": 111},
        )

        # First: send warning
        await checker.check_expiry()
        assert ("user-1", "1d") in checker._warned

        # Now user expires
        user_db.get_expired_users.return_value = ["user-1"]
        user_db.get_users_expiring_within.side_effect = lambda hours: []
        await checker.check_expiry()

        # Warning cleared
        assert ("user-1", "1d") not in checker._warned

    @pytest.mark.asyncio
    async def test_no_warnings_for_non_expiring(self):
        checker, bot, user_db, _ = _make_checker()

        result = await checker.check_expiry()

        assert result == {"expired": [], "warned_3d": [], "warned_1d": []}
        bot.send_message.assert_not_called()


# ------------------------------------------------------------------
# /extend re-activates expired user
# ------------------------------------------------------------------

class TestExtendReactivation:
    @pytest.mark.asyncio
    async def test_extend_reactivates_inactive_user(self):
        user_db = MagicMock()
        user_db.get_user.return_value = UserRecord(
            user_id="user-1",
            display_name="Alice",
            status="inactive",
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        user_db.extend_user_access.return_value = "2025-07-01T00:00:00+00:00"
        orchestrator = MagicMock()
        context = _make_context(args=["user-1", "30"], user_db=user_db, orchestrator=orchestrator)
        update = _make_update()

        await extend_command(update, context)

        user_db.extend_user_access.assert_called_once_with("user-1", 30)
        user_db.set_user_status.assert_called_once_with("user-1", "active")
        orchestrator.activate_user.assert_called_once_with("user-1")
        text = update.message.reply_text.call_args[0][0]
        assert "re-activated" in text.lower()

    @pytest.mark.asyncio
    async def test_extend_does_not_reactivate_active_user(self):
        user_db = MagicMock()
        user_db.get_user.return_value = UserRecord(
            user_id="user-1",
            display_name="Alice",
            status="active",
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        user_db.extend_user_access.return_value = "2025-07-01T00:00:00+00:00"
        orchestrator = MagicMock()
        context = _make_context(args=["user-1", "30"], user_db=user_db, orchestrator=orchestrator)
        update = _make_update()

        await extend_command(update, context)

        user_db.set_user_status.assert_not_called()
        orchestrator.activate_user.assert_not_called()


# ------------------------------------------------------------------
# registered_only blocks expired users (already implemented)
# ------------------------------------------------------------------

class TestRegisteredOnlyBlocksExpired:
    @pytest.mark.asyncio
    async def test_expired_user_blocked(self):
        from src.telegram.middleware import registered_only

        @registered_only
        async def dummy_handler(update, context):
            pass

        user_db = MagicMock()
        user_db.get_user_by_telegram_chat_id.return_value = "user-1"
        # Expiry in the past
        user_db.get_access_expiry.return_value = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()

        update = MagicMock()
        update.effective_chat.id = 111
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot_data = {"user_db": user_db}
        context.user_data = {}

        await dummy_handler(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "expired" in text.lower()
