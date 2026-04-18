"""Tests for the automations subsystem (activity tracker + 4 features)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src.analytics.db import AnalyticsDB
from src.automations.activity_db import ActivityDB
from src.automations.channel_feeler import ChannelFeeler, _render_html
from src.automations.feature_launch import (
    FeatureLaunchBroadcaster,
    _build_dm_text,
    _build_email_html,
)
from src.automations.inactivity_detector import InactivityDetector
from src.automations.value_reminder import ValueReminder, _build_reminder_text
from src.email_bot.db import EmailDB
from src.email_bot.sender import SendResult
from src.verification.db import VerificationDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def activity_db(tmp_path: Path):
    d = ActivityDB(db_path=str(tmp_path / "activity.db"))
    await d.open()
    yield d
    await d.close()


@pytest_asyncio.fixture
async def verification_db(tmp_path: Path):
    d = VerificationDB(db_path=str(tmp_path / "verified.db"))
    await d.open()
    yield d
    await d.close()


@pytest_asyncio.fixture
async def email_db(tmp_path: Path):
    d = EmailDB(db_path=str(tmp_path / "email.db"))
    await d.open()
    yield d
    await d.close()


@pytest_asyncio.fixture
async def analytics_db(tmp_path: Path):
    d = AnalyticsDB(db_path=str(tmp_path / "analytics.db"))
    await d.open()
    yield d
    await d.close()


# ---------------------------------------------------------------------------
# ActivityDB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestActivityDB:
    async def test_record_and_query_last_seen(self, activity_db: ActivityDB):
        await activity_db.record_post("user-a", 1001, when=1000)
        await activity_db.record_post("user-a", 1002, when=2000)
        assert await activity_db.last_seen("user-a") == 2000

    async def test_upsert_keeps_most_recent(self, activity_db: ActivityDB):
        await activity_db.record_post("user-a", 1001, when=2000)
        # Older post shouldn't overwrite
        await activity_db.record_post("user-a", 1001, when=1000)
        assert await activity_db.last_seen("user-a") == 2000

    async def test_count_unique_posters(self, activity_db: ActivityDB):
        await activity_db.record_post("u1", 1001, when=1000)
        await activity_db.record_post("u2", 1001, when=1500)
        await activity_db.record_post("u1", 1001, when=2000)  # duplicate user
        count = await activity_db.count_unique_posters(1001, since_epoch=0)
        assert count == 2

    async def test_count_unique_posters_respects_cutoff(self, activity_db: ActivityDB):
        await activity_db.record_post("u1", 1001, when=1000)
        await activity_db.record_post("u2", 1001, when=3000)
        count = await activity_db.count_unique_posters(1001, since_epoch=2000)
        assert count == 1  # only u2 posted after cutoff

    async def test_users_inactive_since(self, activity_db: ActivityDB):
        await activity_db.record_post("active", 1001, when=5000)
        await activity_db.record_post("inactive", 1001, when=1000)
        result = await activity_db.users_inactive_since(3000)
        assert result == ["inactive"]

    async def test_feeler_cooldown(self, activity_db: ActivityDB):
        assert await activity_db.can_send_feeler(1001, cooldown_seconds=3600) is True
        await activity_db.mark_feeler_sent(1001, when=int(time.time()))
        assert await activity_db.can_send_feeler(1001, cooldown_seconds=3600) is False


# ---------------------------------------------------------------------------
# Feature 1: FeatureLaunchBroadcaster
# ---------------------------------------------------------------------------


class TestFeatureLaunchCopy:
    def test_dm_text_includes_title_and_cta(self):
        text = _build_dm_text("Perp Bot v2", "Covers 50+ tokens now.", "https://whop.com/potion")
        assert "Perp Bot v2" in text
        assert "Covers 50+ tokens" in text
        assert "https://whop.com/potion" in text

    def test_email_html_includes_greeting_and_cta(self):
        html = _build_email_html("Perp Bot v2", "Covers 50+ tokens.", "https://whop.com/potion", "Luke")
        assert "Hey Luke," in html
        assert "Perp Bot v2" in html
        assert "https://whop.com/potion" in html

    def test_email_html_escapes_user_input(self):
        html = _build_email_html("<script>", "<img/onerror=x>", "https://example.com", "")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


@pytest.mark.asyncio
class TestFeatureLaunchBroadcaster:
    async def test_no_active_users_returns_zeroes(
        self, verification_db: VerificationDB,
    ):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        broadcaster = FeatureLaunchBroadcaster(
            telegram_bot=bot,
            verification_db=verification_db,
            resend_client=None,
            telegram_rate_per_sec=1000.0,
            email_rate_per_sec=1000.0,
        )
        stats = await broadcaster.broadcast("title", "description", include_email=False)
        assert stats.dm_attempted == 0
        assert stats.email_attempted == 0
        bot.send_message.assert_not_called()

    async def test_dms_all_active_users(self, verification_db: VerificationDB):
        # Seed users
        await verification_db.upsert_verified(
            telegram_user_id=1, discord_user_id="d1",
            refresh_token_encrypted="t1", email="a@example.com",
        )
        await verification_db.upsert_verified(
            telegram_user_id=2, discord_user_id="d2",
            refresh_token_encrypted="t2", email="b@example.com",
        )

        bot = MagicMock()
        bot.send_message = AsyncMock()
        broadcaster = FeatureLaunchBroadcaster(
            telegram_bot=bot,
            verification_db=verification_db,
            resend_client=None,
            telegram_rate_per_sec=1000.0,
            email_rate_per_sec=1000.0,
        )
        stats = await broadcaster.broadcast(
            "title", "description", include_email=False,
        )
        assert stats.dm_attempted == 2
        assert stats.dm_sent == 2
        assert bot.send_message.await_count == 2

    async def test_email_half_only_users_with_email(
        self, verification_db: VerificationDB,
    ):
        await verification_db.upsert_verified(
            telegram_user_id=1, discord_user_id="d1",
            refresh_token_encrypted="t1", email="a@example.com",
        )
        await verification_db.upsert_verified(
            telegram_user_id=2, discord_user_id="d2",
            refresh_token_encrypted="t2", email="",  # no email
        )

        bot = MagicMock()
        bot.send_message = AsyncMock()
        resend = MagicMock()
        resend.send = AsyncMock(return_value=SendResult(ok=True, resend_id="x"))

        broadcaster = FeatureLaunchBroadcaster(
            telegram_bot=bot,
            verification_db=verification_db,
            resend_client=resend,
            telegram_rate_per_sec=1000.0,
            email_rate_per_sec=1000.0,
        )
        stats = await broadcaster.broadcast(
            "title", "description", include_email=True,
        )
        assert stats.dm_attempted == 2
        assert stats.email_attempted == 1
        assert stats.email_sent == 1


# ---------------------------------------------------------------------------
# Feature 2: InactivityDetector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestInactivityDetector:
    async def test_enrolls_inactive_users_with_email(
        self,
        activity_db: ActivityDB,
        verification_db: VerificationDB,
        email_db: EmailDB,
    ):
        now = int(time.time())
        # User 1: active (recent post)
        await verification_db.upsert_verified(
            telegram_user_id=1, discord_user_id="d1",
            refresh_token_encrypted="t1", email="active@example.com",
        )
        await activity_db.record_post("d1", 1001, when=now - 3600)

        # User 2: inactive (20 days ago) + has email
        await verification_db.upsert_verified(
            telegram_user_id=2, discord_user_id="d2",
            refresh_token_encrypted="t2", email="inactive@example.com",
        )
        await activity_db.record_post("d2", 1001, when=now - 20 * 86400)

        # User 3: inactive but NO email (should be skipped)
        await verification_db.upsert_verified(
            telegram_user_id=3, discord_user_id="d3",
            refresh_token_encrypted="t3", email="",
        )
        await activity_db.record_post("d3", 1001, when=now - 20 * 86400)

        detector = InactivityDetector(
            activity_db=activity_db,
            verification_db=verification_db,
            email_db=email_db,
            threshold_days=14,
        )
        summary = await detector.run_once()

        assert summary["enrolled"] == 1
        # Users with no email are filtered out of the audience before the
        # inactivity check, so they never hit the skipped_no_email bucket.
        assert summary["skipped_no_email"] == 0
        # User 3 (no email) is excluded from the audience entirely.
        assert summary["scanned"] == 2
        # Verify user 2 actually got enrolled
        sub = await email_db.get_subscriber("inactive@example.com")
        assert sub is not None
        assert sub.trigger_type == "inactivity"

    async def test_cooldown_blocks_recent_reenrollment(
        self,
        activity_db: ActivityDB,
        verification_db: VerificationDB,
        email_db: EmailDB,
    ):
        now = int(time.time())
        await verification_db.upsert_verified(
            telegram_user_id=1, discord_user_id="d1",
            refresh_token_encrypted="t1", email="user@example.com",
        )
        await activity_db.record_post("d1", 1001, when=now - 20 * 86400)

        detector = InactivityDetector(
            activity_db=activity_db,
            verification_db=verification_db,
            email_db=email_db,
            threshold_days=14,
            cooldown_days=30,
        )
        first = await detector.run_once()
        assert first["enrolled"] == 1

        # Re-run immediately: should be cooldowned
        second = await detector.run_once()
        assert second["enrolled"] == 0
        assert second["skipped_cooldown"] == 1


# ---------------------------------------------------------------------------
# Feature 3: ValueReminder
# ---------------------------------------------------------------------------


class TestValueReminderCopy:
    def test_text_includes_stats(self):
        text = _build_reminder_text(
            name="Luke",
            calls_30d=45,
            top_pair="PEPE/USDT",
            top_pnl_pct=480.0,
            active_member_count=400,
        )
        assert "Luke" in text
        assert "45" in text
        assert "PEPE/USDT" in text
        assert "480%" in text
        assert "400" in text

    def test_text_handles_empty_name(self):
        text = _build_reminder_text("", 10, "ETH/USDT", 50.0, 100)
        assert "Hey there," in text


@pytest.mark.asyncio
class TestValueReminder:
    async def test_sends_to_due_users(
        self,
        verification_db: VerificationDB,
        analytics_db: AnalyticsDB,
    ):
        now = int(time.time())
        # User 1: verified 40 days ago, never got reminder -> due
        await verification_db.upsert_verified(
            telegram_user_id=1, discord_user_id="d1",
            refresh_token_encrypted="t1",
        )
        # Backdate verified_at via direct SQL
        await verification_db._conn.execute(
            "UPDATE verified_users SET verified_at = ? WHERE telegram_user_id = ?",
            (now - 40 * 86400, 1),
        )
        await verification_db._conn.commit()

        # User 2: just verified today -> not due
        await verification_db.upsert_verified(
            telegram_user_id=2, discord_user_id="d2",
            refresh_token_encrypted="t2",
        )

        bot = MagicMock()
        bot.send_message = AsyncMock()
        reminder = ValueReminder(
            telegram_bot=bot,
            verification_db=verification_db,
            analytics_db=analytics_db,
            cycle_days=30,
            send_rate_per_sec=1000.0,
        )
        summary = await reminder.run_once()

        assert summary["sent"] == 1
        assert summary["skipped_recent"] == 1
        bot.send_message.assert_awaited_once()

    async def test_blocked_user_does_not_crash_cycle(
        self,
        verification_db: VerificationDB,
        analytics_db: AnalyticsDB,
    ):
        from telegram.error import Forbidden

        now = int(time.time())
        await verification_db.upsert_verified(
            telegram_user_id=1, discord_user_id="d1",
            refresh_token_encrypted="t1",
        )
        await verification_db._conn.execute(
            "UPDATE verified_users SET verified_at = ? WHERE telegram_user_id = ?",
            (now - 40 * 86400, 1),
        )
        await verification_db._conn.commit()

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=Forbidden("blocked"))

        reminder = ValueReminder(
            telegram_bot=bot,
            verification_db=verification_db,
            analytics_db=analytics_db,
            send_rate_per_sec=1000.0,
        )
        summary = await reminder.run_once()
        assert summary["failed"] == 1
        # Cycle completed without raising


# ---------------------------------------------------------------------------
# Feature 4: ChannelFeeler
# ---------------------------------------------------------------------------


class TestChannelFeelerCopy:
    def test_render_telegram_bot_variant(self):
        subject, text, html = _render_html("telegram_bot", "https://example.com")
        assert "Telegram" in subject
        assert "https://example.com" in text
        assert "https://example.com" in html

    def test_render_tools_variant(self):
        subject, text, html = _render_html("tools", "https://example.com")
        assert "tools" in subject.lower()

    def test_unknown_variant_falls_back_to_tools(self):
        subject, _, _ = _render_html("nonexistent", "https://example.com")
        assert subject  # doesn't crash


@pytest.mark.asyncio
class TestChannelFeeler:
    async def test_fires_when_engagement_low(
        self,
        activity_db: ActivityDB,
        verification_db: VerificationDB,
    ):
        now = int(time.time())
        # 2 posters in tracked channel (below threshold of 5)
        await activity_db.record_post("d1", 1001, when=now - 3600)
        await activity_db.record_post("d2", 1001, when=now - 7200)

        # 3 users with email
        for i in range(1, 4):
            await verification_db.upsert_verified(
                telegram_user_id=i, discord_user_id=f"d{i}",
                refresh_token_encrypted=f"t{i}",
                email=f"u{i}@example.com",
            )

        resend = MagicMock()
        resend.send = AsyncMock(return_value=SendResult(ok=True, resend_id="x"))

        feeler = ChannelFeeler(
            activity_db=activity_db,
            verification_db=verification_db,
            resend_client=resend,
            variant_by_channel={1001: "tools"},
            low_engagement_threshold=5,
            window_days=14,
            send_rate_per_sec=1000.0,
        )
        summary = await feeler.run_once()

        assert summary["feelers_fired"] == 1
        assert summary["emails_sent"] == 3

    async def test_skips_when_engagement_healthy(
        self,
        activity_db: ActivityDB,
        verification_db: VerificationDB,
    ):
        now = int(time.time())
        # 6 distinct posters (above threshold)
        for i in range(6):
            await activity_db.record_post(f"d{i}", 1001, when=now - 3600)

        await verification_db.upsert_verified(
            telegram_user_id=1, discord_user_id="d1",
            refresh_token_encrypted="t1", email="u@example.com",
        )

        resend = MagicMock()
        resend.send = AsyncMock(return_value=SendResult(ok=True, resend_id="x"))

        feeler = ChannelFeeler(
            activity_db=activity_db,
            verification_db=verification_db,
            resend_client=resend,
            variant_by_channel={1001: "tools"},
            low_engagement_threshold=5,
            window_days=14,
            send_rate_per_sec=1000.0,
        )
        summary = await feeler.run_once()
        assert summary["feelers_fired"] == 0
        assert summary["emails_sent"] == 0
        resend.send.assert_not_called()

    async def test_cooldown_prevents_double_fire(
        self,
        activity_db: ActivityDB,
        verification_db: VerificationDB,
    ):
        now = int(time.time())
        await activity_db.record_post("d1", 1001, when=now - 3600)  # 1 poster (low)
        await verification_db.upsert_verified(
            telegram_user_id=1, discord_user_id="d1",
            refresh_token_encrypted="t1", email="u@example.com",
        )

        resend = MagicMock()
        resend.send = AsyncMock(return_value=SendResult(ok=True, resend_id="x"))

        feeler = ChannelFeeler(
            activity_db=activity_db,
            verification_db=verification_db,
            resend_client=resend,
            variant_by_channel={1001: "tools"},
            low_engagement_threshold=5,
            cooldown_days=30,
            send_rate_per_sec=1000.0,
        )
        first = await feeler.run_once()
        second = await feeler.run_once()
        assert first["feelers_fired"] == 1
        assert second["feelers_fired"] == 0  # cooldown blocked it


# ---------------------------------------------------------------------------
# DiscordListener activity hook
# ---------------------------------------------------------------------------


class TestActivityHookIntegration:
    def test_listener_accepts_activity_hook_params(self):
        """Smoke test that DiscordListener accepts the new constructor params."""
        import asyncio
        from src.discord_listener import DiscordListener

        async def hook(user_id: str, channel_id: int) -> None:
            pass

        listener = DiscordListener(
            bot_token="fake-token",
            monitored_channel_ids={1001, 1002},
            queue=asyncio.Queue(),
            activity_hook=hook,
            activity_channel_ids={2001, 2002},
        )
        # Should not raise
        assert listener.client is not None
