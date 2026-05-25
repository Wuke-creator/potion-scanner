"""Tests for the engagement_score recompute + scoring math."""

from __future__ import annotations

import math
import time
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from src.email_bot.engagement_scoring import (
    CLICK_WEIGHT,
    HALF_LIFE_DAYS,
    HOT_THRESHOLD,
    OPEN_WEIGHT,
    SCORING_WINDOW_SECONDS,
    TIER_COLD,
    TIER_HOT,
    TIER_WARM,
    WARM_THRESHOLD,
    EngagementScore,
    EngagementScoreDB,
    _decay,
    compute_score,
    tier_for_score,
)


# ---------------------------------------------------------------------------
# Pure scoring math
# ---------------------------------------------------------------------------


class TestComputeScore:
    """No DB involved. Just the weighted-sum math.

    The decay is applied by ``recompute_all`` before this function ever
    sees the values; here we only verify that the weighted sum is right.
    """

    def test_zero_when_no_events(self):
        assert compute_score(
            opens_decayed=0.0, clicks_decayed=0.0,
        ) == 0.0

    def test_opens_only(self):
        # 5 decayed opens at weight 1.0 = 5.0.
        assert compute_score(
            opens_decayed=5.0, clicks_decayed=0.0,
        ) == 5.0

    def test_clicks_outweigh_opens(self):
        # 1 click (3) > 2 opens (2).
        assert compute_score(
            opens_decayed=0.0, clicks_decayed=1.0,
        ) > compute_score(
            opens_decayed=2.0, clicks_decayed=0.0,
        )

    def test_replies_weighted_heaviest(self):
        # 1 reply alone hits hot tier (10pt weight).
        score = compute_score(
            opens_decayed=0.0, clicks_decayed=0.0,
            replies_decayed=1.0,
        )
        assert score == 10.0
        assert tier_for_score(score) == TIER_HOT

    def test_full_stack(self):
        # 3 today-fresh opens + 2 today-fresh clicks = 3 + 6 = 9
        score = compute_score(
            opens_decayed=3.0, clicks_decayed=2.0,
        )
        assert score == 9.0


# ---------------------------------------------------------------------------
# Per-event decay function (the math the recompute query implements)
# ---------------------------------------------------------------------------


class TestDecay:
    def test_today_open_is_full_weight(self):
        # decay(0 days) == 1.0, so one open today contributes OPEN_WEIGHT.
        assert _decay(0) == pytest.approx(1.0)
        assert _decay(0) * OPEN_WEIGHT == pytest.approx(1.0)

    def test_open_at_half_life_is_half_weight(self):
        # 14-day-old open scores 0.5pt (1.0 * 0.5)
        assert _decay(HALF_LIFE_DAYS) == pytest.approx(0.5)
        assert _decay(HALF_LIFE_DAYS) * OPEN_WEIGHT == pytest.approx(0.5)

    def test_open_at_two_half_lives_is_quarter_weight(self):
        # 28-day-old open scores 0.25pt
        assert _decay(2 * HALF_LIFE_DAYS) == pytest.approx(0.25)
        assert (
            _decay(2 * HALF_LIFE_DAYS) * OPEN_WEIGHT
            == pytest.approx(0.25)
        )

    def test_click_seven_days_ago_scores_about_2_1pt(self):
        # decay(7) == 2 ** (-0.5) ~= 0.7071
        # 0.7071 * CLICK_WEIGHT (3.0) ~= 2.12
        seven_day_click = _decay(7) * CLICK_WEIGHT
        assert seven_day_click == pytest.approx(3.0 * math.sqrt(0.5))
        assert 2.0 < seven_day_click < 2.2

    def test_compute_score_combines_per_event_decay(self):
        # Real recipient: today's click + 14d-old click + 7d-old open
        # => clicks_decayed = 1.0 + 0.5 = 1.5 -> 4.5pt
        # => opens_decayed  = 0.707         -> 0.707pt
        # total ~= 5.2 (warm)
        clicks_decayed = _decay(0) + _decay(14)
        opens_decayed = _decay(7)
        score = compute_score(
            opens_decayed=opens_decayed,
            clicks_decayed=clicks_decayed,
        )
        assert score == pytest.approx(4.5 + 0.7071, abs=0.01)
        assert tier_for_score(score) == TIER_WARM


# ---------------------------------------------------------------------------
# Tier thresholds
# ---------------------------------------------------------------------------


class TestTierForScore:
    def test_cold_below_warm_threshold(self):
        assert tier_for_score(0) == TIER_COLD
        assert tier_for_score(WARM_THRESHOLD - 1) == TIER_COLD

    def test_warm_between_thresholds(self):
        assert tier_for_score(WARM_THRESHOLD) == TIER_WARM
        assert tier_for_score(HOT_THRESHOLD - 1) == TIER_WARM

    def test_hot_at_or_above_threshold(self):
        assert tier_for_score(HOT_THRESHOLD) == TIER_HOT
        assert tier_for_score(HOT_THRESHOLD + 100) == TIER_HOT


# ---------------------------------------------------------------------------
# DB integration: upsert, get, history, tier_counts, recompute_all
# ---------------------------------------------------------------------------


# Helper: seed an email_events table in the same DB the scoring module uses.
# We use the same DDL the EmailEventsDB module uses so the scoring queries
# work end-to-end against a realistic schema.
_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS email_events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  resend_email_id TEXT NOT NULL,
  broadcast_id    TEXT NOT NULL DEFAULT '',
  recipient       TEXT NOT NULL,
  event_type      TEXT NOT NULL,
  event_at        INTEGER NOT NULL,
  click_url       TEXT,
  bounce_type     TEXT,
  bounce_message  TEXT,
  raw_payload     TEXT NOT NULL DEFAULT '',
  recipient_domain TEXT NOT NULL DEFAULT '',
  UNIQUE(resend_email_id, event_type, event_at)
);
"""


async def _seed_event(
    conn: aiosqlite.Connection,
    *,
    recipient: str,
    event_type: str,
    event_at: int,
    resend_id: str | None = None,
) -> None:
    rid = resend_id or f"re_{event_type}_{event_at}_{recipient}"
    await conn.execute(
        "INSERT OR IGNORE INTO email_events "
        "(resend_email_id, broadcast_id, recipient, event_type, "
        " event_at, raw_payload) VALUES (?, '', ?, ?, ?, '{}')",
        (rid, recipient, event_type, event_at),
    )
    await conn.commit()


@pytest_asyncio.fixture
async def score_db(tmp_path: Path):
    path = tmp_path / "email_events.db"
    # Create events table first via a side connection so the scoring DB's
    # open() doesn't need to know about it.
    side = await aiosqlite.connect(str(path))
    try:
        await side.execute(_EVENTS_DDL)
        await side.commit()
    finally:
        await side.close()

    db = EngagementScoreDB(db_path=str(path))
    await db.open()
    yield db, path
    await db.close()


@pytest.mark.asyncio
class TestEngagementScoreDB:
    async def test_get_returns_none_for_unknown(self, score_db):
        db, _ = score_db
        assert await db.get("ghost@example.com") is None

    async def test_upsert_persists_row(self, score_db):
        db, _ = score_db
        row = EngagementScore(
            email="user@example.com",
            score=12,
            tier=TIER_HOT,
            opens_30d=4,
            clicks_30d=2,
            last_open_at=1_700_000_000,
            last_click_at=1_699_000_000,
            days_since_last_event=2,
            updated_at=1_700_000_000,
        )
        await db.upsert(row)
        got = await db.get("user@example.com")
        assert got is not None
        assert got.score == 12
        assert got.tier == TIER_HOT
        assert got.opens_30d == 4

    async def test_get_is_case_insensitive(self, score_db):
        db, _ = score_db
        row = EngagementScore(
            email="User@Example.com",
            score=5,
            tier=TIER_WARM,
            opens_30d=2,
            clicks_30d=1,
            last_open_at=None,
            last_click_at=None,
            days_since_last_event=None,
            updated_at=int(time.time()),
        )
        await db.upsert(row)
        got = await db.get("USER@EXAMPLE.COM")
        assert got is not None
        assert got.tier == TIER_WARM

    async def test_upsert_records_tier_transition_in_history(self, score_db):
        db, _ = score_db
        row1 = EngagementScore(
            email="user@example.com", score=2, tier=TIER_COLD,
            opens_30d=2, clicks_30d=0,
            last_open_at=None, last_click_at=None,
            days_since_last_event=None,
            updated_at=1_700_000_000,
        )
        transitioned = await db.upsert(row1)
        assert transitioned is True  # first row counts as transition

        row2 = EngagementScore(
            email="user@example.com", score=3, tier=TIER_COLD,
            opens_30d=3, clicks_30d=0,
            last_open_at=None, last_click_at=None,
            days_since_last_event=None,
            updated_at=1_700_000_001,
        )
        transitioned = await db.upsert(row2)
        assert transitioned is False  # same tier, no transition

        row3 = EngagementScore(
            email="user@example.com", score=8, tier=TIER_WARM,
            opens_30d=5, clicks_30d=1,
            last_open_at=None, last_click_at=None,
            days_since_last_event=None,
            updated_at=1_700_000_002,
        )
        transitioned = await db.upsert(row3)
        assert transitioned is True

    async def test_prior_tier_walks_history(self, score_db):
        db, _ = score_db
        now = 1_700_000_000
        # Record three transitions over time.
        for ts, tier, score in [
            (now - 60 * 86400, TIER_HOT, 12),
            (now - 25 * 86400, TIER_WARM, 5),
            (now - 1 * 86400, TIER_COLD, 1),
        ]:
            await db.upsert(EngagementScore(
                email="user@example.com",
                score=score, tier=tier,
                opens_30d=0, clicks_30d=0,
                last_open_at=None, last_click_at=None,
                days_since_last_event=None,
                updated_at=ts,
            ))
        # Patch time.time so prior_tier's cutoff math is deterministic.
        import src.email_bot.engagement_scoring as m
        orig = m.time.time
        m.time.time = lambda: now
        try:
            # As-of -20d: latest history row at-or-before is the -25d WARM row.
            assert await db.prior_tier("user@example.com", days_ago=20) == TIER_WARM
            # As-of -50d: only the -60d HOT row is at-or-before.
            assert await db.prior_tier("user@example.com", days_ago=50) == TIER_HOT
            # As-of -999d: no row at-or-before, fallback to oldest = HOT.
            assert await db.prior_tier("user@example.com", days_ago=999) == TIER_HOT
        finally:
            m.time.time = orig

    async def test_tier_counts(self, score_db):
        db, _ = score_db
        now = int(time.time())
        await db.upsert(EngagementScore(
            email="hot@x.com", score=12, tier=TIER_HOT,
            opens_30d=4, clicks_30d=2,
            last_open_at=None, last_click_at=None,
            days_since_last_event=None, updated_at=now,
        ))
        await db.upsert(EngagementScore(
            email="warm@x.com", score=5, tier=TIER_WARM,
            opens_30d=3, clicks_30d=0,
            last_open_at=None, last_click_at=None,
            days_since_last_event=None, updated_at=now,
        ))
        await db.upsert(EngagementScore(
            email="cold@x.com", score=0, tier=TIER_COLD,
            opens_30d=0, clicks_30d=0,
            last_open_at=None, last_click_at=None,
            days_since_last_event=None, updated_at=now,
        ))
        counts = await db.tier_counts()
        assert counts == {TIER_HOT: 1, TIER_WARM: 1, TIER_COLD: 1}


# ---------------------------------------------------------------------------
# recompute_all integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRecomputeAll:
    async def test_no_events_no_rows(self, score_db):
        db, _ = score_db
        summary = await db.recompute_all(now=1_700_000_000)
        assert summary["updated"] == 0
        assert summary["tier_distribution"] == {
            TIER_HOT: 0, TIER_WARM: 0, TIER_COLD: 0,
        }

    async def test_recompute_classifies_hot_user(self, score_db):
        db, path = score_db
        now = 1_700_000_000
        # Seed a realistic "hot" recipient: several recent opens plus a
        # couple of recent clicks. With decay, today's events dominate;
        # the further out we go the less they contribute.
        side = await aiosqlite.connect(str(path))
        try:
            # 4 opens spread over the last 4 days.
            for i in range(4):
                await _seed_event(
                    side, recipient="alice@example.com",
                    event_type="opened",
                    event_at=now - i * 86400,
                )
            # 3 clicks: today, 2d ago, 5d ago.
            for ago in (0, 2, 5):
                await _seed_event(
                    side, recipient="alice@example.com",
                    event_type="clicked",
                    event_at=now - ago * 86400,
                )
        finally:
            await side.close()

        summary = await db.recompute_all(now=now)
        assert summary["updated"] == 1

        row = await db.get("alice@example.com")
        assert row is not None
        # Recompute by hand with the same decay math to verify the SQL.
        expected_opens = sum(_decay(i) for i in range(4))
        expected_clicks = _decay(0) + _decay(2) + _decay(5)
        expected_score = (
            expected_opens * OPEN_WEIGHT
            + expected_clicks * CLICK_WEIGHT
        )
        assert row.opens_30d == pytest.approx(expected_opens, abs=0.001)
        assert row.clicks_30d == pytest.approx(expected_clicks, abs=0.001)
        assert row.score == pytest.approx(expected_score, abs=0.001)
        # And the resulting tier crosses HOT_THRESHOLD comfortably.
        assert row.score >= HOT_THRESHOLD
        assert row.tier == TIER_HOT

    async def test_recompute_warm_user_realistic_sequence(self, score_db):
        # A realistic warm-tier sequence: a few recent opens and a
        # couple of clicks within the half-life window. Should land
        # between WARM and HOT thresholds with the decay model.
        db, path = score_db
        now = 1_700_000_000
        side = await aiosqlite.connect(str(path))
        try:
            # Two opens in the last week.
            await _seed_event(
                side, recipient="warm@example.com",
                event_type="opened",
                event_at=now - 2 * 86400, resend_id="re_w_o1",
            )
            await _seed_event(
                side, recipient="warm@example.com",
                event_type="opened",
                event_at=now - 6 * 86400, resend_id="re_w_o2",
            )
            # Two clicks: one 5 days ago, one 10 days ago. Both inside
            # the half-life window so they still carry meaningful weight.
            await _seed_event(
                side, recipient="warm@example.com",
                event_type="clicked",
                event_at=now - 5 * 86400, resend_id="re_w_c1",
            )
            await _seed_event(
                side, recipient="warm@example.com",
                event_type="clicked",
                event_at=now - 10 * 86400, resend_id="re_w_c2",
            )
        finally:
            await side.close()

        await db.recompute_all(now=now)
        row = await db.get("warm@example.com")
        assert row is not None
        # WARM_THRESHOLD <= score < HOT_THRESHOLD
        assert WARM_THRESHOLD <= row.score < HOT_THRESHOLD
        assert row.tier == TIER_WARM

    async def test_recompute_old_events_score_low(self, score_db):
        # Same number of opens, but all near the 30d window edge: heavily
        # decayed, should land cold. This is the whole point of switching
        # to decay-weighted scoring.
        db, path = score_db
        now = 1_700_000_000
        side = await aiosqlite.connect(str(path))
        try:
            for i in range(5):
                # ~28-day-old opens. decay(28) = 0.25, so 5 of them ~= 1.25.
                await _seed_event(
                    side, recipient="stale_opens@example.com",
                    event_type="opened",
                    event_at=now - 28 * 86400 + i * 3600,
                    resend_id=f"re_stale_{i}",
                )
        finally:
            await side.close()

        await db.recompute_all(now=now)
        row = await db.get("stale_opens@example.com")
        assert row is not None
        assert row.score < WARM_THRESHOLD
        assert row.tier == TIER_COLD

    async def test_recompute_ignores_events_outside_window(self, score_db):
        db, path = score_db
        now = 1_700_000_000
        far_past = now - SCORING_WINDOW_SECONDS - 86400
        side = await aiosqlite.connect(str(path))
        try:
            # Stale events 31+ days ago should not count.
            await _seed_event(
                side, recipient="stale@example.com",
                event_type="clicked",
                event_at=far_past,
            )
        finally:
            await side.close()
        summary = await db.recompute_all(now=now)
        # Stale-only recipient should not appear in the score table.
        assert summary["updated"] == 0
        assert await db.get("stale@example.com") is None
