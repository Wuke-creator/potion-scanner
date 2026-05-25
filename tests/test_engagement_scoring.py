"""Tests for the engagement_score recompute + scoring math."""

from __future__ import annotations

import time
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from src.email_bot.engagement_scoring import (
    EngagementScore,
    EngagementScoreDB,
    HOT_THRESHOLD,
    SCORING_WINDOW_SECONDS,
    TIER_COLD,
    TIER_HOT,
    TIER_WARM,
    WARM_THRESHOLD,
    compute_score,
    tier_for_score,
)


# ---------------------------------------------------------------------------
# Pure scoring math
# ---------------------------------------------------------------------------


class TestComputeScore:
    """No DB involved. Just the math."""

    def test_zero_when_no_events(self):
        assert compute_score(
            opens=0, clicks=0,
            last_open_at=None, last_click_at=None,
            now=1_700_000_000,
        ) == 0

    def test_opens_only(self):
        # 5 opens * 1pt = 5
        assert compute_score(
            opens=5, clicks=0,
            last_open_at=None, last_click_at=None,
            now=1_700_000_000,
        ) == 5

    def test_clicks_outweigh_opens(self):
        # 1 click (3) > 2 opens (2)
        assert compute_score(
            opens=0, clicks=1,
            last_open_at=None, last_click_at=None,
            now=1_700_000_000,
        ) > compute_score(
            opens=2, clicks=0,
            last_open_at=None, last_click_at=None,
            now=1_700_000_000,
        )

    def test_recent_open_bonus_applies(self):
        now = 1_700_000_000
        # Open 1 day ago -> +2 bonus on top of the 1pt open value.
        with_bonus = compute_score(
            opens=1, clicks=0,
            last_open_at=now - 86400, last_click_at=None,
            now=now,
        )
        assert with_bonus == 1 + 2

    def test_recent_open_bonus_does_not_apply_past_window(self):
        now = 1_700_000_000
        # Open 10 days ago: outside 7-day recency window.
        no_bonus = compute_score(
            opens=1, clicks=0,
            last_open_at=now - 10 * 86400, last_click_at=None,
            now=now,
        )
        assert no_bonus == 1

    def test_recent_click_bonus_applies(self):
        now = 1_700_000_000
        # Click 7 days ago -> +5 bonus on top of 3pt click value.
        with_bonus = compute_score(
            opens=0, clicks=1,
            last_open_at=None, last_click_at=now - 7 * 86400,
            now=now,
        )
        assert with_bonus == 3 + 5

    def test_recent_click_bonus_does_not_apply_past_window(self):
        now = 1_700_000_000
        no_bonus = compute_score(
            opens=0, clicks=1,
            last_open_at=None, last_click_at=now - 30 * 86400,
            now=now,
        )
        assert no_bonus == 3

    def test_full_stack(self):
        # 3 opens (+3), 2 clicks (+6), open 1d ago (+2), click 5d ago (+5)
        # = 16, comfortably hot
        now = 1_700_000_000
        score = compute_score(
            opens=3, clicks=2,
            last_open_at=now - 86400, last_click_at=now - 5 * 86400,
            now=now,
        )
        assert score == 16
        assert tier_for_score(score) == TIER_HOT

    def test_replies_weighted_heaviest(self):
        # 1 reply alone hits hot tier
        score = compute_score(
            opens=0, clicks=0,
            last_open_at=None, last_click_at=None,
            now=1_700_000_000,
            replies=1,
        )
        assert score == 10
        assert tier_for_score(score) == TIER_HOT


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
        # Seed events directly on the same DB.
        side = await aiosqlite.connect(str(path))
        try:
            for i in range(3):
                await _seed_event(
                    side, recipient="alice@example.com",
                    event_type="opened",
                    event_at=now - (i + 1) * 86400,
                )
            for i in range(2):
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
        # 3 opens (3) + 2 clicks (6) + recent open (+2) + recent click (+5)
        # = 16, hot.
        assert row.score == 16
        assert row.tier == TIER_HOT
        assert row.opens_30d == 3
        assert row.clicks_30d == 2

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
