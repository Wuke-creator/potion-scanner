"""Tests for Step 12 — error handling, /cancel, group chat protection, rate limiting.

Covers:
- Global error handler sends friendly message
- /cancel clears state and confirms
- DM-only filter blocks group chats
- Rate limiter blocks after 30 commands/minute
- Unknown command handler (already existed, verify here)
"""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram.ext import ApplicationHandlerStop

from src.telegram.handlers.help import cancel_command, unknown_command
from src.telegram.middleware import (
    _user_timestamps,
    check_rate_limit,
    dm_only_filter,
    global_error_handler,
    rate_limit_filter,
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_update(chat_type="private", has_message=True):
    update = MagicMock()
    update.effective_chat.type = chat_type
    update.effective_user.id = 12345
    if has_message:
        update.message = AsyncMock()
        update.message.reply_text = AsyncMock()
    else:
        update.message = None
    return update


def _make_context():
    context = MagicMock()
    context.user_data = {"some_key": "some_value"}
    context.error = Exception("test error")
    return context


# ------------------------------------------------------------------
# Global error handler
# ------------------------------------------------------------------

class TestGlobalErrorHandler:
    @pytest.mark.asyncio
    async def test_sends_friendly_message(self):
        update = _make_update()
        context = _make_context()

        await global_error_handler(update, context)

        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "something went wrong" in text.lower()

    @pytest.mark.asyncio
    async def test_handles_non_update(self):
        """Should not crash when update is not an Update object."""
        context = _make_context()
        await global_error_handler("not-an-update", context)  # no crash

    @pytest.mark.asyncio
    async def test_handles_update_without_message(self):
        update = _make_update(has_message=False)
        context = _make_context()
        await global_error_handler(update, context)  # no crash


# ------------------------------------------------------------------
# /cancel command
# ------------------------------------------------------------------

class TestCancelCommand:
    @pytest.mark.asyncio
    async def test_cancel_clears_state(self):
        update = _make_update()
        context = _make_context()

        await cancel_command(update, context)

        assert context.user_data == {}
        text = update.message.reply_text.call_args[0][0]
        assert "cancelled" in text.lower() or "canceled" in text.lower()

    @pytest.mark.asyncio
    async def test_cancel_with_empty_state(self):
        update = _make_update()
        context = _make_context()
        context.user_data = {}

        await cancel_command(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "cancel" in text.lower()


# ------------------------------------------------------------------
# DM-only filter
# ------------------------------------------------------------------

class TestDmOnlyFilter:
    @pytest.mark.asyncio
    async def test_private_chat_passes(self):
        update = _make_update(chat_type="private")
        context = _make_context()

        # Should not raise
        await dm_only_filter(update, context)

    @pytest.mark.asyncio
    async def test_group_chat_blocked(self):
        update = _make_update(chat_type="group")
        context = _make_context()

        with pytest.raises(ApplicationHandlerStop):
            await dm_only_filter(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "private" in text.lower()

    @pytest.mark.asyncio
    async def test_supergroup_blocked(self):
        update = _make_update(chat_type="supergroup")
        context = _make_context()

        with pytest.raises(ApplicationHandlerStop):
            await dm_only_filter(update, context)

    @pytest.mark.asyncio
    async def test_group_no_message(self):
        """Callback query from group — block without reply."""
        update = _make_update(chat_type="group", has_message=False)
        context = _make_context()

        with pytest.raises(ApplicationHandlerStop):
            await dm_only_filter(update, context)


# ------------------------------------------------------------------
# Rate limiting
# ------------------------------------------------------------------

class TestRateLimiting:
    def setup_method(self):
        """Clear rate limit state before each test."""
        _user_timestamps.clear()

    def test_within_limit(self):
        for _ in range(30):
            assert check_rate_limit(111) is True

    def test_exceeds_limit(self):
        for _ in range(30):
            check_rate_limit(111)
        assert check_rate_limit(111) is False

    def test_different_users_independent(self):
        for _ in range(30):
            check_rate_limit(111)
        # User 222 should still be fine
        assert check_rate_limit(222) is True

    def test_window_expires(self):
        """After the window passes, user can send again."""
        for _ in range(30):
            check_rate_limit(111)
        assert check_rate_limit(111) is False

        # Simulate time passing by clearing old timestamps
        _user_timestamps[111] = [time.monotonic() - 61]
        assert check_rate_limit(111) is True

    @pytest.mark.asyncio
    async def test_rate_limit_filter_blocks(self):
        # Fill up the limit
        for _ in range(30):
            check_rate_limit(111)

        update = _make_update()
        update.effective_user.id = 111
        context = _make_context()

        with pytest.raises(ApplicationHandlerStop):
            await rate_limit_filter(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "too fast" in text.lower()

    @pytest.mark.asyncio
    async def test_rate_limit_filter_passes(self):
        update = _make_update()
        update.effective_user.id = 222
        context = _make_context()

        # Should not raise
        await rate_limit_filter(update, context)


# ------------------------------------------------------------------
# Unknown command handler
# ------------------------------------------------------------------

class TestUnknownCommand:
    @pytest.mark.asyncio
    async def test_unknown_command(self):
        update = _make_update()
        context = _make_context()

        await unknown_command(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "unknown command" in text.lower()
        assert "/help" in text
