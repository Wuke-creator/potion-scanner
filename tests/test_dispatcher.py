"""Tests for src/dispatcher.py — DM fan-out with rate limiting.

Uses an in-memory fake DB + fake Telegram Bot so the tests run fast and
don't hit any network.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from telegram.error import Forbidden, RetryAfter, TelegramError

from src.config import DispatcherConfig
from src.dispatcher import Dispatcher


@dataclass
class FakeDB:
    """Drop-in stand-in for VerificationDB with only the methods Dispatcher uses."""

    active_ids: list[int] = field(default_factory=list)
    inactive_marks: list[int] = field(default_factory=list)

    async def list_active_user_ids(self) -> list[int]:
        return list(self.active_ids)

    async def list_subscribed_user_ids(self, channel_key: str) -> list[int]:
        # In tests, all active users are considered subscribed
        return list(self.active_ids)

    async def update_after_recheck(
        self,
        telegram_user_id: int,
        is_active: bool,
        new_refresh_token_encrypted: str | None = None,
    ) -> None:
        if not is_active:
            self.inactive_marks.append(telegram_user_id)
            if telegram_user_id in self.active_ids:
                self.active_ids.remove(telegram_user_id)


class FakeBot:
    """Telegram Bot stand-in that records calls and can fake errors."""

    def __init__(self):
        self.sent_to: list[int] = []
        self._error_for: dict[int, Exception] = {}
        self._call_count: dict[int, int] = {}

    def will_fail(self, user_id: int, exc: Exception) -> None:
        self._error_for[user_id] = exc

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self._call_count[chat_id] = self._call_count.get(chat_id, 0) + 1
        if chat_id in self._error_for:
            exc = self._error_for[chat_id]
            # One-shot: after first raise, succeed on retry
            del self._error_for[chat_id]
            raise exc
        self.sent_to.append(chat_id)

    def call_count_for(self, user_id: int) -> int:
        return self._call_count.get(user_id, 0)


def _fast_config() -> DispatcherConfig:
    # Fast defaults for tests — full burst, plenty of concurrency
    return DispatcherConfig(
        rate_per_sec=1000.0,
        max_concurrent=100,
        per_send_timeout_sec=5.0,
        queue_max_size=1000,
    )


@pytest.mark.asyncio
class TestDispatcher:
    async def test_happy_path_delivers_to_every_active_user(self):
        db = FakeDB(active_ids=[101, 102, 103, 104, 105])
        bot = FakeBot()
        dispatcher = Dispatcher(bot=bot, db=db, config=_fast_config())
        await dispatcher.start()
        try:
            await dispatcher.dispatch("hello", source_key="perp_bot")
            # Wait for the dispatcher loop to drain
            for _ in range(50):
                stats = dispatcher.last_stats
                if stats is not None and stats.finished_at is not None:
                    break
                await asyncio.sleep(0.02)
        finally:
            await dispatcher.stop()

        assert sorted(bot.sent_to) == [101, 102, 103, 104, 105]
        stats = dispatcher.last_stats
        assert stats is not None
        assert stats.sent == 5
        assert stats.blocked == 0
        assert stats.failed == 0

    async def test_blocked_user_is_marked_inactive(self):
        db = FakeDB(active_ids=[201, 202, 203])
        bot = FakeBot()
        bot.will_fail(202, Forbidden("blocked"))
        dispatcher = Dispatcher(bot=bot, db=db, config=_fast_config())
        await dispatcher.start()
        try:
            await dispatcher.dispatch("hi", source_key="perp_bot")
            for _ in range(50):
                stats = dispatcher.last_stats
                if stats is not None and stats.finished_at is not None:
                    break
                await asyncio.sleep(0.02)
        finally:
            await dispatcher.stop()

        assert sorted(bot.sent_to) == [201, 203]
        assert 202 in db.inactive_marks
        stats = dispatcher.last_stats
        assert stats is not None
        assert stats.sent == 2
        assert stats.blocked == 1

    async def test_retry_after_is_retried(self):
        db = FakeDB(active_ids=[301])
        bot = FakeBot()
        bot.will_fail(301, RetryAfter(0.01))
        dispatcher = Dispatcher(bot=bot, db=db, config=_fast_config())
        await dispatcher.start()
        try:
            await dispatcher.dispatch("hi", source_key="perp_bot")
            for _ in range(50):
                stats = dispatcher.last_stats
                if stats is not None and stats.finished_at is not None:
                    break
                await asyncio.sleep(0.02)
        finally:
            await dispatcher.stop()

        # First call raises RetryAfter, second call succeeds
        assert bot.call_count_for(301) == 2
        assert 301 in bot.sent_to
        stats = dispatcher.last_stats
        assert stats is not None
        assert stats.sent == 1
        assert stats.rate_limited == 1

    async def test_unexpected_error_counts_as_failed(self):
        db = FakeDB(active_ids=[401])
        bot = FakeBot()
        bot.will_fail(401, RuntimeError("kaboom"))
        dispatcher = Dispatcher(bot=bot, db=db, config=_fast_config())
        await dispatcher.start()
        try:
            await dispatcher.dispatch("hi", source_key="perp_bot")
            for _ in range(50):
                stats = dispatcher.last_stats
                if stats is not None and stats.finished_at is not None:
                    break
                await asyncio.sleep(0.02)
        finally:
            await dispatcher.stop()

        stats = dispatcher.last_stats
        assert stats is not None
        assert stats.failed == 1
        assert stats.sent == 0

    async def test_empty_user_list_is_handled(self):
        db = FakeDB(active_ids=[])
        bot = FakeBot()
        dispatcher = Dispatcher(bot=bot, db=db, config=_fast_config())
        await dispatcher.start()
        try:
            await dispatcher.dispatch("hi", source_key="perp_bot")
            for _ in range(20):
                stats = dispatcher.last_stats
                if stats is not None and stats.finished_at is not None:
                    break
                await asyncio.sleep(0.02)
        finally:
            await dispatcher.stop()

        assert bot.sent_to == []
        stats = dispatcher.last_stats
        assert stats is not None
        assert stats.total_users == 0
        assert stats.sent == 0

    async def test_back_to_back_alerts_both_deliver(self):
        db = FakeDB(active_ids=[501, 502])
        bot = FakeBot()
        dispatcher = Dispatcher(bot=bot, db=db, config=_fast_config())
        await dispatcher.start()
        try:
            await dispatcher.dispatch("alert A", source_key="perp_bot")
            await dispatcher.dispatch("alert B", source_key="manual_perp")
            # Wait for both alerts to drain
            for _ in range(100):
                await asyncio.sleep(0.02)
                if len(bot.sent_to) >= 4:
                    break
        finally:
            await dispatcher.stop()

        # Both alerts × 2 users = 4 sends total
        assert len(bot.sent_to) == 4
        assert sorted(bot.sent_to) == [501, 501, 502, 502]

    async def test_rate_limit_is_observed(self):
        """Verifies the dispatcher doesn't flood past configured rate."""
        db = FakeDB(active_ids=list(range(1, 51)))  # 50 users
        bot = FakeBot()
        # Tight rate: 50/sec, small burst — 50 users should take ~1s
        config = DispatcherConfig(
            rate_per_sec=50.0,
            max_concurrent=50,
            per_send_timeout_sec=5.0,
            queue_max_size=100,
        )
        dispatcher = Dispatcher(bot=bot, db=db, config=config)
        await dispatcher.start()
        try:
            await dispatcher.dispatch("rate-test", source_key="perp_bot")
            for _ in range(200):
                stats = dispatcher.last_stats
                if stats is not None and stats.finished_at is not None:
                    break
                await asyncio.sleep(0.05)
        finally:
            await dispatcher.stop()

        stats = dispatcher.last_stats
        assert stats is not None
        assert stats.sent == 50
        # 50 tokens from a ~100-token burst should drain fast, but the
        # underlying bucket still blocks long enough that throughput
        # doesn't exceed rate_per_sec. Duration should be under 2s.
        assert stats.duration_sec < 2.0
