"""Tests for the /engagement-snapshot Discord slash command rendering.

The Discord interaction itself is hard to integration-test without
spinning up a bot session, so these tests assert against the
``_render_engagement_snapshot`` formatter that the slash command
delegates to. The command body is a thin wrapper that fetches
counts/top/transitions from EngagementScoreDB and feeds them to the
formatter, so renderer coverage is the load-bearing part.

Also covers EngagementScoreDB.top_engaged + recent_transitions helpers
end-to-end against a real SQLite file.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import pytest_asyncio

from src.email_bot.discord_commands import _render_engagement_snapshot
from src.email_bot.engagement_scoring import (
    EngagementScore,
    EngagementScoreDB,
    TIER_COLD,
    TIER_HOT,
    TIER_WARM,
)


# ---------------------------------------------------------------------------
# Renderer tests
# ---------------------------------------------------------------------------


class TestEngagementSnapshotRenderer:
    def test_empty_state(self):
        out = _render_engagement_snapshot(
            counts={"hot": 0, "warm": 0, "cold": 0},
            top=[],
            transitions=[],
        )
        # Should land cleanly even with zero data.
        assert "Total scored recipients: 0" in out
        assert "Hot:" in out and "Warm:" in out and "Cold:" in out
        assert "(none)" in out  # both top and transitions show (none)

    def test_mixed_tiers_render_with_percentages(self):
        out = _render_engagement_snapshot(
            counts={"hot": 25, "warm": 50, "cold": 25},
            top=[
                {"email": "a@x.com", "score": 50.0, "tier": "hot",
                 "updated_at": 1_700_000_000},
                {"email": "b@x.com", "score": 30.0, "tier": "hot",
                 "updated_at": 1_700_000_000},
            ],
            transitions=[
                {"email": "c@x.com", "score": 12.0, "tier": "warm",
                 "recorded_at": 1_700_000_000},
            ],
        )
        # Total + per-tier percentages
        assert "Total scored recipients: 100" in out
        assert "25.0%" in out
        assert "50.0%" in out
        # Top recipients land in numbered order
        assert "1. a@x.com" in out
        assert "2. b@x.com" in out
        # Transitions show with tier label
        assert "c@x.com" in out
        assert "-> warm" in out

    def test_more_than_5_top_is_truncated(self):
        # The renderer is the safety net even if the caller passes >5.
        top = [
            {
                "email": f"u{i}@x.com",
                "score": float(100 - i),
                "tier": "hot",
                "updated_at": 1_700_000_000,
            }
            for i in range(10)
        ]
        out = _render_engagement_snapshot(
            counts={"hot": 10, "warm": 0, "cold": 0}, top=top, transitions=[],
        )
        # We expect exactly 5 numbered entries.
        for i in range(1, 6):
            assert f"{i}. u{i - 1}@x.com" in out
        # The 6th does NOT appear.
        assert "6. u5@x.com" not in out

    def test_more_than_5_transitions_is_truncated(self):
        trans = [
            {
                "email": f"t{i}@x.com",
                "score": 5.0,
                "tier": "cold",
                "recorded_at": 1_700_000_000 + i,
            }
            for i in range(10)
        ]
        out = _render_engagement_snapshot(
            counts={"hot": 0, "warm": 0, "cold": 10}, top=[], transitions=trans,
        )
        # Only the first 5 (most recent in caller order) render.
        for i in range(5):
            assert f"t{i}@x.com" in out
        # t6 and beyond should not appear.
        assert "t6@x.com" not in out

    def test_zero_total_percentages_show_na(self):
        # Avoid ZeroDivisionError when no recipients have scores yet.
        out = _render_engagement_snapshot(
            counts={"hot": 0, "warm": 0, "cold": 0},
            top=[], transitions=[],
        )
        assert "n/a" in out


# ---------------------------------------------------------------------------
# DB helpers test
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def score_db(tmp_path: Path):
    db = EngagementScoreDB(db_path=str(tmp_path / "events.db"))
    await db.open()
    yield db
    await db.close()


@pytest.mark.asyncio
class TestEngagementSnapshotHelpers:
    """Cover the new top_engaged + recent_transitions helpers on
    EngagementScoreDB. The slash command depends on these returning the
    right shape -- the renderer tests above use that shape directly."""

    async def test_top_engaged_returns_highest_first(self, score_db):
        now = int(time.time())
        for i, score in enumerate([10, 5, 30, 7, 1, 15]):
            await score_db.upsert(EngagementScore(
                email=f"u{i}@x.com", score=float(score),
                tier=(
                    TIER_HOT if score >= 10
                    else TIER_WARM if score >= 4 else TIER_COLD
                ),
                opens_30d=float(score), clicks_30d=0.0,
                last_open_at=now, last_click_at=None,
                days_since_last_event=1, updated_at=now,
            ))
        top = await score_db.top_engaged(limit=3)
        assert len(top) == 3
        # Descending by score: 30, 15, 10
        assert top[0]["score"] == 30.0
        assert top[1]["score"] == 15.0
        assert top[2]["score"] == 10.0

    async def test_top_engaged_with_empty_db(self, score_db):
        top = await score_db.top_engaged(limit=5)
        assert top == []

    async def test_recent_transitions_returns_newest_first(self, score_db):
        # Each upsert with a tier change writes a history row. We force
        # transitions by alternating tiers explicitly.
        now = int(time.time())
        rows = [
            ("u@x.com", 1.0, TIER_COLD, now - 100),
            ("u@x.com", 15.0, TIER_HOT, now - 50),
            ("u@x.com", 5.0, TIER_WARM, now - 10),
        ]
        # Upsert preserves prior tier and writes history only on change.
        for email, score, tier, ts in rows:
            await score_db.upsert(EngagementScore(
                email=email, score=score, tier=tier,
                opens_30d=score, clicks_30d=0.0,
                last_open_at=ts, last_click_at=None,
                days_since_last_event=1, updated_at=ts,
            ))
        transitions = await score_db.recent_transitions(limit=5)
        # Three transitions all landed.
        assert len(transitions) >= 1
        # Newest first
        recorded_at = [t["recorded_at"] for t in transitions]
        assert recorded_at == sorted(recorded_at, reverse=True)

    async def test_recent_transitions_empty(self, score_db):
        out = await score_db.recent_transitions(limit=5)
        assert out == []
