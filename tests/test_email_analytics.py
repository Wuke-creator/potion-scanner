"""Tests for email analytics: resend_id linkage, sequence_stats,
Day 0 funnel, and UTM rewriting.

These cover the four phases shipped 2026-05-04 to make the email
subsystem observable per-sequence rather than only per-broadcast.
"""

from __future__ import annotations

import time
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from src.email_bot.analytics import EmailAnalytics
from src.email_bot.db import EmailDB, Subscriber
from src.email_bot.events_db import EmailEventsDB
from src.email_bot.templates import _apply_utm, render
from src.email_bot.stats import StatsBundle


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def email_db(tmp_path: Path):
    d = EmailDB(db_path=str(tmp_path / "email.db"))
    await d.open()
    yield d
    await d.close()


@pytest_asyncio.fixture
async def events_db(tmp_path: Path):
    d = EmailEventsDB(db_path=str(tmp_path / "email_events.db"))
    await d.open()
    yield d
    await d.close()


@pytest_asyncio.fixture
async def verified_db_path(tmp_path: Path):
    """Bare-bones verified_users.db with the columns the funnel reads."""
    path = tmp_path / "verified.db"
    async with aiosqlite.connect(str(path)) as conn:
        await conn.execute(
            "CREATE TABLE verified_users ("
            "  telegram_user_id INTEGER PRIMARY KEY,"
            "  email TEXT NOT NULL DEFAULT '',"
            "  verified_at INTEGER NOT NULL"
            ")"
        )
        await conn.commit()
    return str(path)


def _sub(email: str = "user@example.com") -> Subscriber:
    return Subscriber(
        email=email, name="Luke", trigger_type="onboarding",
        exit_reason="none", rejoin_url="https://whop.com/potion",
        created_at=int(time.time()),
    )


def _stats_bundle() -> StatsBundle:
    return StatsBundle(
        calls_7d_total=10,
        wins_7d_over_50pct=2,
        top_call_7d={"pair": "ETH/USDT", "pnl_pct": 50.0, "days_ago": 1},
        top_calls_7d=[
            {"pair": "ETH/USDT", "pnl_pct": 50.0, "days_ago": 1},
        ],
        calls_30d_total=40,
        top_call_30d={"pair": "BTC/USDT", "pnl_pct": 100.0, "days_ago": 10},
    )


# ---------------------------------------------------------------------------
# Phase 1: resend_id linkage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestResendIdLinkage:
    async def test_mark_sent_persists_resend_id(self, email_db: EmailDB):
        await email_db.upsert_subscriber(_sub())
        send_id = await email_db.schedule_one(
            email="user@example.com", sequence="onboarding", day=0,
            due_at=int(time.time()) - 60,
        )
        await email_db.mark_sent(send_id, resend_id="rs_abc123")

        rows = await email_db.sent_in_window(sequence="onboarding", day=0)
        assert len(rows) == 1
        assert rows[0]["resend_id"] == "rs_abc123"
        assert rows[0]["sequence"] == "onboarding"
        assert rows[0]["day"] == 0

    async def test_mark_sent_without_resend_id_is_excluded_from_window(
        self, email_db: EmailDB,
    ):
        """Pre-Phase-1 sends (resend_id NULL) are skipped by sent_in_window
        because there's no way to join them against email_events. They'd
        only inflate the denominator."""
        await email_db.upsert_subscriber(_sub())
        send_id = await email_db.schedule_one(
            email="user@example.com", sequence="winback", day=1,
            due_at=int(time.time()) - 60,
        )
        await email_db.mark_sent(send_id)  # no resend_id passed

        rows = await email_db.sent_in_window(sequence="winback", day=1)
        assert rows == []

    async def test_sent_in_window_filters_by_time(self, email_db: EmailDB):
        await email_db.upsert_subscriber(_sub())
        now = int(time.time())
        old_id = await email_db.schedule_one(
            email="user@example.com", sequence="onboarding", day=0,
            due_at=now - 100 * 86400,
        )
        recent_id = await email_db.schedule_one(
            email="user@example.com", sequence="onboarding", day=0,
            due_at=now - 1 * 86400,
        )
        await email_db.mark_sent(old_id, resend_id="rs_old")
        await email_db.mark_sent(recent_id, resend_id="rs_recent")
        # mark_sent always stamps sent_at = time.time() at call site, so we
        # backdate the older row directly to simulate a real historical
        # send that landed 100 days ago.
        await email_db._conn.execute(
            "UPDATE scheduled_sends SET sent_at = ? WHERE id = ?",
            (now - 100 * 86400, old_id),
        )
        await email_db._conn.commit()

        # Last 7 days should only catch the recent one
        rows = await email_db.sent_in_window(
            sequence="onboarding", day=0,
            since=now - 7 * 86400, until=now + 60,
        )
        assert len(rows) == 1
        assert rows[0]["resend_id"] == "rs_recent"


# ---------------------------------------------------------------------------
# Phase 2: aggregate_by_resend_ids + sequence_stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAggregateByResendIds:
    async def test_empty_input_returns_zeros(self, events_db: EmailEventsDB):
        stats = await events_db.aggregate_by_resend_ids(set())
        assert stats["sent"] == 0
        assert stats["delivered"] == 0
        assert stats["delivery_rate"] == 0.0
        assert stats["top_clicked_urls"] == []

    async def test_aggregate_counts_events_for_matching_ids(
        self, events_db: EmailEventsDB,
    ):
        ts = int(time.time())
        # rs_a: sent + delivered + opened + clicked
        # rs_b: sent + delivered + bounced (hard)
        # rs_c: sent + delivered + opened (no click)
        # rs_d: NOT in our set, should be excluded
        for rid, etype, click_url, bounce_type in [
            ("rs_a", "sent",      None, None),
            ("rs_a", "delivered", None, None),
            ("rs_a", "opened",    None, None),
            ("rs_a", "clicked",   "https://whop.com/potion", None),
            ("rs_b", "sent",      None, None),
            ("rs_b", "delivered", None, None),
            ("rs_b", "bounced",   None, "hard"),
            ("rs_c", "sent",      None, None),
            ("rs_c", "delivered", None, None),
            ("rs_c", "opened",    None, None),
            ("rs_d", "sent",      None, None),
        ]:
            await events_db.record_event(
                resend_email_id=rid, broadcast_id="",
                recipient=f"{rid}@example.com",
                event_type=etype, event_at=ts,
                click_url=click_url, bounce_type=bounce_type,
                bounce_message=None, raw_payload="{}",
            )
            ts += 1  # event_at must differ for the UNIQUE constraint

        stats = await events_db.aggregate_by_resend_ids(
            ["rs_a", "rs_b", "rs_c"],
        )
        assert stats["sent"] == 3
        assert stats["delivered"] == 3
        assert stats["opened"] == 2
        assert stats["clicked"] == 1
        assert stats["bounced"] == 1
        assert stats["hard_bounced"] == 1
        assert stats["soft_bounced"] == 0
        assert stats["unique_openers"] == 2
        assert stats["unique_clickers"] == 1
        assert stats["delivery_rate"] == pytest.approx(1.0)
        assert stats["open_rate"] == pytest.approx(2 / 3)
        assert stats["click_rate_opened"] == pytest.approx(1 / 2)
        assert any(
            row["url"] == "https://whop.com/potion"
            for row in stats["top_clicked_urls"]
        )


@pytest.mark.asyncio
class TestSequenceStats:
    async def test_sequence_stats_joins_email_db_and_events(
        self,
        email_db: EmailDB,
        events_db: EmailEventsDB,
    ):
        await email_db.upsert_subscriber(_sub())
        now = int(time.time())
        # Two Day 0 onboarding sends, both with resend_ids
        ids = []
        for i, rid in enumerate(("rs_x", "rs_y")):
            sid = await email_db.schedule_one(
                email="user@example.com", sequence="onboarding",
                day=0, due_at=now - 60 - i,
            )
            await email_db.mark_sent(sid, resend_id=rid)
            ids.append(rid)

        # Webhook fires for rs_x: delivered + opened + clicked
        # Webhook fires for rs_y: delivered (only)
        events = [
            ("rs_x", "delivered", None, now),
            ("rs_x", "opened",    None, now + 1),
            ("rs_x", "clicked",   "https://whop.com/potion", now + 2),
            ("rs_y", "delivered", None, now + 3),
        ]
        for rid, etype, url, when in events:
            await events_db.record_event(
                resend_email_id=rid, broadcast_id="",
                recipient="user@example.com",
                event_type=etype, event_at=when,
                click_url=url, bounce_type=None,
                bounce_message=None, raw_payload="{}",
            )

        analytics = EmailAnalytics(email_db, events_db)
        stats = await analytics.sequence_stats(
            sequence="onboarding", day=0, days_back=30,
        )
        assert stats["sequence"] == "onboarding"
        assert stats["day"] == 0
        # email.db is authoritative for "sent": 2 rows with resend_id set
        assert stats["sent"] == 2
        assert stats["delivered"] == 2
        assert stats["opened"] == 1
        assert stats["clicked"] == 1
        # Rates use the email.db denominator
        assert stats["delivery_rate"] == pytest.approx(1.0)
        # webhook 'sent' events: 0 (not recorded), so sent_events should be set
        assert stats["sent_events"] == 0

    async def test_sequence_stats_with_no_sends_returns_zeros(
        self, email_db: EmailDB, events_db: EmailEventsDB,
    ):
        analytics = EmailAnalytics(email_db, events_db)
        stats = await analytics.sequence_stats(
            sequence="winback", day=1, days_back=7,
        )
        assert stats["sent"] == 0
        assert stats["delivered"] == 0
        assert stats["delivery_rate"] == 0.0


# ---------------------------------------------------------------------------
# Phase 3: Day 0 funnel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDay0Funnel:
    async def test_funnel_with_no_data(
        self,
        email_db: EmailDB,
        events_db: EmailEventsDB,
        verified_db_path: str,
    ):
        analytics = EmailAnalytics(
            email_db, events_db, verified_users_db_path=verified_db_path,
        )
        funnel = await analytics.onboarding_day0_funnel()
        assert funnel["sent"] == 0
        assert funnel["telegram_verified"] == 0
        assert funnel["verify_rate"] == 0.0

    async def test_funnel_counts_verified_in_window(
        self,
        email_db: EmailDB,
        events_db: EmailEventsDB,
        verified_db_path: str,
    ):
        now = int(time.time())
        # Three recipients: one verifies in window, one too late, one never
        recipients = ("a@x.com", "b@x.com", "c@x.com")

        # Seed Day 0 sends for each
        for em, rid in zip(recipients, ("rs_a", "rs_b", "rs_c")):
            sub = Subscriber(
                email=em, name="", trigger_type="onboarding",
                exit_reason="none",
                rejoin_url="https://whop.com/potion",
                created_at=now,
            )
            await email_db.upsert_subscriber(sub)
            sid = await email_db.schedule_one(
                email=em, sequence="onboarding", day=0,
                due_at=now - 5 * 86400,
            )
            # mark_sent stamps sent_at = time.time() at call site, so the
            # test's conversion-window math works against "now-ish".
            await email_db.mark_sent(sid, resend_id=rid)
            # All three got delivered + opened
            for etype in ("delivered", "opened"):
                await events_db.record_event(
                    resend_email_id=rid, broadcast_id="",
                    recipient=em, event_type=etype, event_at=now,
                    click_url=None, bounce_type=None,
                    bounce_message=None, raw_payload="{}",
                )

        # verified_users:
        #   a@x.com: verified 2 days after send (in window)
        #   b@x.com: verified 60 days after send (out of window)
        #   c@x.com: never verified
        async with aiosqlite.connect(verified_db_path) as conn:
            await conn.execute(
                "INSERT INTO verified_users "
                "(telegram_user_id, email, verified_at) VALUES (?, ?, ?)",
                (1, "a@x.com", now + 2 * 86400),
            )
            await conn.execute(
                "INSERT INTO verified_users "
                "(telegram_user_id, email, verified_at) VALUES (?, ?, ?)",
                (2, "b@x.com", now + 60 * 86400),
            )
            await conn.commit()

        analytics = EmailAnalytics(
            email_db, events_db, verified_users_db_path=verified_db_path,
        )
        funnel = await analytics.onboarding_day0_funnel(
            days_back=30, conversion_window_days=7,
        )
        assert funnel["sent"] == 3
        assert funnel["delivered"] == 3
        assert funnel["opened"] == 3
        # Only a@x.com verified within 7 days
        assert funnel["telegram_verified"] == 1
        assert funnel["verify_rate"] == pytest.approx(1 / 3)


# ---------------------------------------------------------------------------
# Phase 4: UTM rewriting
# ---------------------------------------------------------------------------


class TestApplyUtm:
    def test_tags_whop_url(self):
        body = "Manage your sub at https://whop.com/potion to continue."
        out = _apply_utm(body, "winback", 5)
        assert "utm_source=potion_email" in out
        assert "utm_campaign=winback_day5" in out
        # Original URL still present (params appended)
        assert "https://whop.com/potion" in out

    def test_tags_discord_and_telegram(self):
        body = (
            "Discord: https://discord.com/channels/123/456 "
            "Telegram: https://t.me/PotionScannerBot"
        )
        out = _apply_utm(body, "onboarding", 0)
        assert out.count("utm_campaign=onboarding_day0") == 2

    def test_idempotent_on_pre_tagged_url(self):
        url = (
            "https://whop.com/potion?utm_source=potion_email"
            "&utm_medium=email&utm_campaign=winback_day1"
        )
        out = _apply_utm(url, "winback", 1)
        # Should not double-append
        assert out.count("utm_source=potion_email") == 1

    def test_double_render_is_idempotent(self):
        body = "https://whop.com/potion"
        once = _apply_utm(body, "onboarding", 3)
        twice = _apply_utm(once, "onboarding", 3)
        assert once == twice

    def test_preserves_existing_query_string(self):
        body = "https://whop.com/potion?ref=abc"
        out = _apply_utm(body, "winback", 7)
        assert "ref=abc" in out
        assert "&utm_source=" in out

    def test_strips_trailing_punctuation_then_re_appends(self):
        body = "Visit https://discord.gg/PotionAlpha. Then start trading."
        out = _apply_utm(body, "onboarding", 0)
        # The period should still be at the end of the URL's surrounding
        # sentence, not inside the query string
        assert "PotionAlpha?utm_source=potion_email&utm_medium=email" in out
        assert "utm_campaign=onboarding_day0. Then" in out

    def test_does_not_tag_external_domain(self):
        body = "Read more at https://example.com/foo"
        out = _apply_utm(body, "winback", 1)
        assert "utm_source" not in out

    def test_render_applies_utm_to_day0(self):
        """End-to-end: the render() dispatcher applies UTMs."""
        email = render("onboarding", 0, _sub(), _stats_bundle())
        assert "utm_campaign=onboarding_day0" in email.text
        assert "utm_campaign=onboarding_day0" in email.html
