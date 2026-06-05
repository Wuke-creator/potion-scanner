"""Tests for the engagement_score recompute + scoring math."""

from __future__ import annotations

import time
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from src.email_bot.engagement_scoring import (
    CLICK_HALF_LIFE_DAYS,
    CLICK_WEIGHT,
    EngagementScore,
    EngagementScoreDB,
    HOT_THRESHOLD,
    OPEN_HALF_LIFE_DAYS,
    OPEN_WEIGHT,
    REPLY_WEIGHT,
    SCORING_WINDOW_SECONDS,
    TIER_COLD,
    TIER_HOT,
    TIER_WARM,
    WARM_THRESHOLD,
    compute_score,
    decayed_value,
    score_from_event_ages,
    tier_for_score,
)


# ---------------------------------------------------------------------------
# Pure scoring math
# ---------------------------------------------------------------------------


class TestComputeScore:
    """No DB involved. Just the decay math."""

    def test_zero_when_no_events(self):
        assert compute_score() == 0.0

    def test_open_at_age_zero_weights_full(self):
        # Single open landed just now -> full OPEN_WEIGHT.
        assert compute_score(
            open_decayed_sum=decayed_value(
                age_days=0, half_life_days=OPEN_HALF_LIFE_DAYS,
            ),
        ) == pytest.approx(OPEN_WEIGHT)

    def test_open_at_half_life_halves_contribution(self):
        score = compute_score(
            open_decayed_sum=decayed_value(
                age_days=OPEN_HALF_LIFE_DAYS,
                half_life_days=OPEN_HALF_LIFE_DAYS,
            ),
        )
        assert score == pytest.approx(OPEN_WEIGHT * 0.5)

    def test_click_decays_slower_than_open_at_same_age(self):
        age = 7.0
        open_part = OPEN_WEIGHT * decayed_value(
            age_days=age, half_life_days=OPEN_HALF_LIFE_DAYS,
        )
        click_part = CLICK_WEIGHT * decayed_value(
            age_days=age, half_life_days=CLICK_HALF_LIFE_DAYS,
        )
        # Clicks: weight 3, half-life 14 (so age=7 gives 0.707 of weight).
        # Opens: weight 1, half-life 7 (so age=7 gives 0.5 of weight).
        assert click_part > open_part

    def test_score_from_event_ages_hot_user(self):
        # Heavy engagement: 6 opens at ~1d + 4 clicks at ~2d. Under the
        # new decay model the score lands well past HOT_THRESHOLD; the
        # old fixture (3 opens + 2 clicks) lands warm now.
        score = score_from_event_ages(
            open_ages_days=[1.0] * 6,
            click_ages_days=[2.0] * 4,
        )
        assert score > HOT_THRESHOLD
        assert tier_for_score(score) == TIER_HOT

    def test_score_falls_with_age(self):
        recent = score_from_event_ages(click_ages_days=[1.0])
        old = score_from_event_ages(click_ages_days=[28.0])
        assert recent > old

    def test_replies_weighted_heaviest(self):
        # One fresh reply alone hits hot tier.
        score = score_from_event_ages(reply_ages_days=[0.0])
        assert score == pytest.approx(REPLY_WEIGHT)
        assert tier_for_score(score) == TIER_HOT


class TestDecayedValue:
    def test_zero_age_returns_one(self):
        assert decayed_value(
            age_days=0, half_life_days=7,
        ) == pytest.approx(1.0)

    def test_half_life_halves(self):
        assert decayed_value(
            age_days=7, half_life_days=7,
        ) == pytest.approx(0.5)

    def test_two_half_lives_quarters(self):
        assert decayed_value(
            age_days=14, half_life_days=7,
        ) == pytest.approx(0.25)

    def test_negative_age_clamps_to_one(self):
        # Clock skew safety: a future event shouldn't blow the score up.
        assert decayed_value(
            age_days=-1, half_life_days=7,
        ) == pytest.approx(1.0)


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
        # Seed a heavily-engaged recipient: 6 opens + 4 clicks all
        # inside the last few days. Under exponential decay this
        # comfortably exceeds HOT_THRESHOLD; the old "3 opens 2 clicks"
        # fixture now lands warm.
        side = await aiosqlite.connect(str(path))
        try:
            for i in range(6):
                await _seed_event(
                    side, recipient="alice@example.com",
                    event_type="opened",
                    event_at=now - (i + 1) * 86400,
                )
            for i in range(4):
                await _seed_event(
                    side, recipient="alice@example.com",
                    event_type="clicked",
                    event_at=now - (i + 1) * 86400,
                )
        finally:
            await side.close()

        summary = await db.recompute_all(now=now)
        assert summary["updated"] == 1

        row = await db.get("alice@example.com")
        assert row is not None
        assert row.tier == TIER_HOT
        assert row.opens_30d == 6
        assert row.clicks_30d == 4
        # Cross-check the SQL-side decayed sum against the Python-side
        # scorer so any drift in the SQL math gets caught.
        expected = score_from_event_ages(
            open_ages_days=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            click_ages_days=[1.0, 2.0, 3.0, 4.0],
        )
        assert row.score == pytest.approx(expected, rel=1e-3)
        assert row.score >= HOT_THRESHOLD

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
