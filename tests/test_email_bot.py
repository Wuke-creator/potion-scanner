"""Tests for the email bot subsystem."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
import pytest_asyncio

from src.email_bot.db import EmailDB, Subscriber
from src.email_bot.stats import StatsBundle, gather_stats
from src.email_bot.templates import render
from src.email_bot.webhook import normalize_reason, _whop_signature_ok


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def email_db(tmp_path: Path):
    d = EmailDB(db_path=str(tmp_path / "email.db"))
    await d.open()
    yield d
    await d.close()


def _sub(email: str = "user@example.com", reason: str = "too_expensive") -> Subscriber:
    return Subscriber(
        email=email, name="Luke", trigger_type="cancellation",
        exit_reason=reason, rejoin_url="https://whop.com/potion",
        created_at=int(time.time()),
    )


def _stats() -> StatsBundle:
    return StatsBundle(
        calls_7d_total=24,
        wins_7d_over_50pct=3,
        top_call_7d={"pair": "ETH/USDT", "pnl_pct": 180.0, "days_ago": 2},
        top_calls_7d=[
            {"pair": "PEPE/USDT", "pnl_pct": 480.0, "days_ago": 1},
            {"pair": "ETH/USDT", "pnl_pct": 180.0, "days_ago": 2},
            {"pair": "SOL/USDT", "pnl_pct": 120.0, "days_ago": 4},
        ],
        calls_30d_total=89,
        top_call_30d={"pair": "BONK/USDT", "pnl_pct": 1150.0, "days_ago": 25},
    )


# ---------------------------------------------------------------------------
# EmailDB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestEmailDBSubscribers:
    async def test_upsert_and_get(self, email_db: EmailDB):
        await email_db.upsert_subscriber(_sub())
        row = await email_db.get_subscriber("user@example.com")
        assert row is not None
        assert row.name == "Luke"
        assert row.exit_reason == "too_expensive"

    async def test_upsert_overwrites(self, email_db: EmailDB):
        await email_db.upsert_subscriber(_sub(reason="too_expensive"))
        await email_db.upsert_subscriber(_sub(reason="market_slow"))
        row = await email_db.get_subscriber("user@example.com")
        assert row is not None
        assert row.exit_reason == "market_slow"

    async def test_rejects_unknown_reason(self, email_db: EmailDB):
        bad = _sub(reason="definitely_not_a_real_reason")
        with pytest.raises(ValueError, match="exit_reason"):
            await email_db.upsert_subscriber(bad)


@pytest.mark.asyncio
class TestEmailDBScheduling:
    async def test_schedule_sequence_creates_rows(self, email_db: EmailDB):
        """Winback and reengagement both run 3 emails at days 1/4/7."""
        await email_db.upsert_subscriber(_sub())
        winback_ids = await email_db.schedule_sequence(
            "user@example.com", "winback",
        )
        assert len(winback_ids) == 3
        # Reengagement now matches winback cadence (3 emails)
        await email_db.schedule_sequence("user@example.com", "reengagement")
        counts = await email_db.count_by_status()
        assert counts.get("pending", 0) == 3

    async def test_schedule_sequence_cancels_previous(self, email_db: EmailDB):
        await email_db.upsert_subscriber(_sub())
        await email_db.schedule_sequence("user@example.com", "winback")
        await email_db.schedule_sequence("user@example.com", "reengagement")
        counts = await email_db.count_by_status()
        # 3 original winback pending sends got canceled + 3 reengagement queued
        assert counts.get("canceled", 0) == 3
        assert counts.get("pending", 0) == 3

    async def test_due_sends_only_returns_pending_and_past_due(self, email_db: EmailDB):
        await email_db.upsert_subscriber(_sub())
        now = int(time.time())
        # Manually schedule one past-due, one future
        await email_db.schedule_one(
            email="user@example.com", sequence="winback", day=1, due_at=now - 60,
        )
        await email_db.schedule_one(
            email="user@example.com", sequence="winback", day=3, due_at=now + 3600,
        )
        due = await email_db.due_sends()
        assert len(due) == 1
        assert due[0].day == 1

    async def test_mark_sent_then_not_due(self, email_db: EmailDB):
        await email_db.upsert_subscriber(_sub())
        now = int(time.time())
        send_id = await email_db.schedule_one(
            email="user@example.com", sequence="winback", day=1, due_at=now - 60,
        )
        await email_db.mark_sent(send_id)
        due = await email_db.due_sends()
        assert due == []

    async def test_count_by_status(self, email_db: EmailDB):
        await email_db.upsert_subscriber(_sub())
        now = int(time.time())
        a = await email_db.schedule_one("user@example.com", "winback", 1, now - 60)
        await email_db.schedule_one("user@example.com", "winback", 3, now + 60)
        await email_db.mark_sent(a)
        counts = await email_db.count_by_status()
        assert counts.get("sent", 0) == 1
        assert counts.get("pending", 0) == 1


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


class TestTemplates:
    @pytest.mark.parametrize("day", [1, 4, 7])
    def test_winback_every_day_renders(self, day: int):
        """New 2026-04-18 cadence: winback fires on days 1, 4, 7."""
        email = render("winback", day, _sub(), _stats())
        assert email.subject
        assert email.text
        assert email.html.startswith("<")
        # Name should appear in the body
        assert "Luke" in email.text

    def test_winback_day5_legacy_still_renders(self):
        """Day 5 was deprecated 2026-04-18 but the legacy renderer stays
        registered so in-flight pending day=5 sends don't crash."""
        email = render("winback", 5, _sub(reason="too_expensive"), _stats())
        assert email.subject

    @pytest.mark.parametrize("day", [1, 4, 7])
    def test_reengagement_every_day_renders(self, day: int):
        """New 2026-04-18 cadence: reengagement also fires on days 1, 4, 7."""
        email = render("reengagement", day, _sub(), _stats())
        assert email.subject
        assert email.text
        assert email.html.startswith("<")

    def test_reengagement_day3_and_day5_legacy_still_render(self):
        """Days 3 and 5 were deprecated 2026-04-18 but their renderers stay
        mapped so in-flight pending sends scheduled before the change
        don't crash on delivery."""
        for day in (3, 5):
            email = render("reengagement", day, _sub(), _stats())
            assert email.subject

    def test_day1_winback_includes_live_stats(self):
        email = render("winback", 1, _sub(), _stats())
        # wins_7d_over_50pct value is rendered as the first bullet
        assert "3 calls hit over 50%+" in email.text

    def test_day5_winback_segments_by_reason(self):
        base_stats = _stats()
        too_expensive = render("winback", 5, _sub(reason="too_expensive"), base_stats)
        market_slow = render("winback", 5, _sub(reason="market_slow"), base_stats)
        alt = render("winback", 5, _sub(reason="found_alternative"), base_stats)

        # Each reason produces a distinct subject + offer copy
        assert "$79" in too_expensive.text
        assert "pause" in market_slow.text.lower()
        assert "compare" in alt.text.lower()

    def test_day5_winback_unknown_reason_falls_back_to_offer_f(self):
        # "fulfillment" should render the Offer F fallback (30% off 2 months)
        email = render("winback", 5, _sub(reason="fulfillment"), _stats())
        assert "30%" in email.text

    def test_html_escapes_subscriber_name(self):
        """Subscriber names flow from Whop and could theoretically contain
        HTML. Verify they're escaped before being injected into the HTML
        body of the email (Day 1 winback uses name in the greeting)."""
        malicious = Subscriber(
            email="user@example.com",
            name="<script>alert(1)</script>",
            trigger_type="cancellation",
            exit_reason="other",
            rejoin_url="https://whop.com/potion",
            created_at=int(time.time()),
        )
        email = render("winback", 1, malicious, _stats())
        assert "<script>alert(1)</script>" not in email.html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in email.html

    def test_unknown_sequence_raises(self):
        with pytest.raises(ValueError):
            render("not-a-real-sequence", 1, _sub(), _stats())

    def test_unknown_day_raises(self):
        with pytest.raises(ValueError):
            render("winback", 99, _sub(), _stats())


# ---------------------------------------------------------------------------
# Webhook helpers
# ---------------------------------------------------------------------------


class TestReasonNormalization:
    @pytest.mark.parametrize("raw,expected", [
        ("Too expensive", "too_expensive"),
        ("too_expensive", "too_expensive"),
        ("Market is slow / taking a break", "market_slow"),
        ("Not using it enough", "not_using"),
        ("Quality of calls declined", "quality_declined"),
        ("Found a better alternative", "found_alternative"),
        ("Fulfilment issue", "other"),  # note: different spelling falls through
        ("Fulfillment issue", "fulfillment"),
        ("", "other"),
        (None, "other"),
        ("random garbage", "other"),
    ])
    def test_normalize(self, raw, expected):
        assert normalize_reason(raw) == expected


class TestWhopSignature:
    def test_valid_signature_passes(self):
        secret = "topsecret"
        body = b'{"email":"u@example.com"}'
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert _whop_signature_ok(body, secret, sig) is True

    def test_wrong_signature_fails(self):
        body = b'{"email":"u@example.com"}'
        sig = "deadbeef" * 8
        assert _whop_signature_ok(body, "topsecret", sig) is False

    def test_empty_secret_fails(self):
        body = b'{}'
        sig = hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
        assert _whop_signature_ok(body, "", sig) is False

    def test_signature_case_insensitive(self):
        secret = "topsecret"
        body = b'{"k":"v"}'
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest().upper()
        assert _whop_signature_ok(body, secret, sig) is True


# ---------------------------------------------------------------------------
# Stats (integration with analytics.db)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGatherStats:
    async def test_gather_against_seeded_analytics_db(self, tmp_path: Path):
        # Seed a tiny analytics DB with one trade + one win event
        from src.analytics.db import AnalyticsDB

        db_path = str(tmp_path / "analytics.db")
        adb = AnalyticsDB(db_path=db_path)
        await adb.open()
        await adb.record_signal(
            trade_id=1, channel_key="perp_bot", pair="ETH/USDT",
            side="LONG", entry=3200.0, leverage=10,
        )
        await adb.record_event(
            trade_id=1, channel_key="perp_bot", event_type="tp_hit",
            tp_number=1, pnl_pct=85.0,
        )
        await adb.close()

        stats = await gather_stats(db_path)
        assert stats.calls_7d_total == 1
        assert stats.wins_7d_over_50pct == 1
        assert stats.top_call_7d is not None
        assert stats.top_call_7d["pair"] == "ETH/USDT"
        assert stats.top_call_7d["pnl_pct"] == pytest.approx(85.0)

    async def test_gather_empty_analytics_returns_zeros(self, tmp_path: Path):
        from src.analytics.db import AnalyticsDB

        db_path = str(tmp_path / "analytics.db")
        adb = AnalyticsDB(db_path=db_path)
        await adb.open()
        await adb.close()

        stats = await gather_stats(db_path)
        assert stats.calls_7d_total == 0
        assert stats.wins_7d_over_50pct == 0
        assert stats.top_call_7d is None
        assert stats.top_calls_7d == []
