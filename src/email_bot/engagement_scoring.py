"""Engagement scoring: derive a per-recipient hot/warm/cold tier from
Resend webhook events.

Lives in ``data/email_events.db`` alongside the raw ``email_events`` log,
so the scoring cron can do everything in one DB without cross-file joins.
A separate table (not a view) so reads from the branch evaluator are O(1)
on the lookup path and the cron pays the cost once per night.

Scoring model (researched against Klaviyo / Customer.io / Braze conventions
plus standard RFM lead-scoring point systems):

    score = (opens_30d * 1)
          + (clicks_30d * 3)
          + (replies_30d * 10)           # not yet captured; reserved
          + (last_open within 7d  ? +2 : 0)
          + (last_click within 14d ? +5 : 0)

    tier:
        score >= 10  -> hot
        score >=  4  -> warm
        otherwise    -> cold

Tier thresholds match the consensus of the deep-research sources: anyone
with at least one recent click or two recent opens is "hot"; anyone with
recent opens but no clicks is "warm"; everyone else is "cold". The
specific point values are a starting position. Tune after ~6 weeks of
real production data lands in the table.

The branch evaluator (``branch_evaluator.py``) is the only consumer of
this table. The worker is intentionally NOT yet touching it. Until the
worker is wired in, scoring runs silently and changes no user-facing
behaviour.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------

TIER_HOT = "hot"
TIER_WARM = "warm"
TIER_COLD = "cold"

# Score floor for each tier. Anyone below WARM_THRESHOLD is cold.
HOT_THRESHOLD = 10
WARM_THRESHOLD = 4

# Window in which events count toward the score. 30 days is the standard
# email engagement window (sources: GlockApps RFM, Klaviyo predictive
# segments). Anything older is treated as inactive history, not signal.
SCORING_WINDOW_SECONDS = 30 * 24 * 3600

# Recency bonuses.
RECENT_OPEN_BONUS_WINDOW = 7 * 24 * 3600   # last open in 7d -> +2
RECENT_CLICK_BONUS_WINDOW = 14 * 24 * 3600  # last click in 14d -> +5
RECENT_OPEN_BONUS_POINTS = 2
RECENT_CLICK_BONUS_POINTS = 5

# Point weights for each event.
OPEN_POINTS = 1
CLICK_POINTS = 3
REPLY_POINTS = 10  # reserved; not currently captured in email_events


@dataclass(frozen=True)
class EngagementScore:
    """One row of the engagement_score table."""
    email: str
    score: int
    tier: str
    opens_30d: int
    clicks_30d: int
    last_open_at: int | None
    last_click_at: int | None
    days_since_last_event: int | None
    updated_at: int


def tier_for_score(score: int) -> str:
    """Pure function: pick the tier label for a numeric score."""
    if score >= HOT_THRESHOLD:
        return TIER_HOT
    if score >= WARM_THRESHOLD:
        return TIER_WARM
    return TIER_COLD


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_SCORE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS engagement_score (
  email                  TEXT PRIMARY KEY,
  score                  INTEGER NOT NULL DEFAULT 0,
  tier                   TEXT NOT NULL DEFAULT 'cold',
  opens_30d              INTEGER NOT NULL DEFAULT 0,
  clicks_30d             INTEGER NOT NULL DEFAULT 0,
  last_open_at           INTEGER,
  last_click_at          INTEGER,
  days_since_last_event  INTEGER,
  updated_at             INTEGER NOT NULL
);
"""

_SCORE_TIER_INDEX = """
CREATE INDEX IF NOT EXISTS idx_engagement_score_tier
    ON engagement_score(tier);
"""

# Captures the prior tier for "previously hot but now quiet" branching
# in the reengagement and inactive_day10 sequences. Updated by the cron
# whenever the tier transitions; the most recent rows show the trajectory.
_SCORE_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS engagement_score_history (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  email       TEXT NOT NULL,
  score       INTEGER NOT NULL,
  tier        TEXT NOT NULL,
  recorded_at INTEGER NOT NULL
);
"""

_SCORE_HISTORY_INDEX = """
CREATE INDEX IF NOT EXISTS idx_engagement_score_history_email
    ON engagement_score_history(email, recorded_at DESC);
"""


# ---------------------------------------------------------------------------
# EngagementScoreDB wrapper
# ---------------------------------------------------------------------------


class EngagementScoreDB:
    """Async SQLite wrapper for the engagement_score + history tables.

    Reuses the same DB file as ``EmailEventsDB`` (``data/email_events.db``)
    so the recompute cron can scan ``email_events`` and write
    ``engagement_score`` in the same connection.
    """

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(_SCORE_TABLE_DDL)
        await self._conn.execute(_SCORE_TIER_INDEX)
        await self._conn.execute(_SCORE_HISTORY_DDL)
        await self._conn.execute(_SCORE_HISTORY_INDEX)
        await self._conn.commit()
        logger.info("EngagementScoreDB opened at %s", self._db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---- reads --------------------------------------------------------

    async def get(self, email: str) -> EngagementScore | None:
        """Look up one recipient's current score. Lowercased match so
        we don't care about case at the call site."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT email, score, tier, opens_30d, clicks_30d, "
            "       last_open_at, last_click_at, days_since_last_event, "
            "       updated_at "
            "FROM engagement_score "
            "WHERE LOWER(email) = LOWER(?)",
            (email,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return EngagementScore(
            email=row[0],
            score=int(row[1]),
            tier=str(row[2]),
            opens_30d=int(row[3]),
            clicks_30d=int(row[4]),
            last_open_at=int(row[5]) if row[5] is not None else None,
            last_click_at=int(row[6]) if row[6] is not None else None,
            days_since_last_event=(
                int(row[7]) if row[7] is not None else None
            ),
            updated_at=int(row[8]),
        )

    async def prior_tier(
        self, email: str, *, days_ago: int,
    ) -> str | None:
        """Return the tier this recipient had ``days_ago`` days ago.

        Used by reengagement / inactive_day10 branching: "was this user
        previously hot, or never engaged in the first place?". Falls back
        to the oldest history row if no row exists at the exact cutoff.
        Returns None if we've never recorded a tier for this email.
        """
        assert self._conn is not None
        cutoff = int(time.time()) - (max(0, int(days_ago)) * 24 * 3600)
        # Find the most recent history row at or before the cutoff.
        async with self._conn.execute(
            "SELECT tier FROM engagement_score_history "
            "WHERE LOWER(email) = LOWER(?) AND recorded_at <= ? "
            "ORDER BY recorded_at DESC LIMIT 1",
            (email, cutoff),
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            return str(row[0])
        # No row at-or-before the cutoff. Try the oldest row we have so
        # callers always get a usable answer if there's any history.
        async with self._conn.execute(
            "SELECT tier FROM engagement_score_history "
            "WHERE LOWER(email) = LOWER(?) "
            "ORDER BY recorded_at ASC LIMIT 1",
            (email,),
        ) as cursor:
            row = await cursor.fetchone()
        return str(row[0]) if row else None

    async def tier_counts(self) -> dict[str, int]:
        """Distribution snapshot: how many recipients are hot / warm / cold
        right now. For dashboard + sanity checks."""
        assert self._conn is not None
        out = {TIER_HOT: 0, TIER_WARM: 0, TIER_COLD: 0}
        async with self._conn.execute(
            "SELECT tier, COUNT(*) FROM engagement_score GROUP BY tier",
        ) as cursor:
            async for row in cursor:
                if row[0] in out:
                    out[row[0]] = int(row[1])
        return out

    async def top_engaged(self, limit: int = 5) -> list[dict]:
        """Return the most-engaged recipients by score (highest first).

        Each row is a dict {email, score, tier, updated_at}. Used by
        the /engagement-snapshot Discord command to spotlight the
        recipients driving the bulk of the engagement.
        """
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT email, score, tier, updated_at "
            "FROM engagement_score "
            "ORDER BY score DESC, updated_at DESC LIMIT ?",
            (int(limit),),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "email": r[0],
                "score": float(r[1]),
                "tier": str(r[2]),
                "updated_at": int(r[3]),
            }
            for r in rows
        ]

    async def recent_transitions(self, limit: int = 5) -> list[dict]:
        """Return the most-recent tier transitions across all recipients.

        Each row is a dict {email, score, tier, recorded_at}. The
        ``engagement_score_history`` table only gets a row when the
        tier actually changed, so this is the right place to watch
        the trajectory of the audience.
        """
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT email, score, tier, recorded_at "
            "FROM engagement_score_history "
            "ORDER BY recorded_at DESC LIMIT ?",
            (int(limit),),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "email": r[0],
                "score": float(r[1]),
                "tier": str(r[2]),
                "recorded_at": int(r[3]),
            }
            for r in rows
        ]

    # ---- writes -------------------------------------------------------

    async def upsert(self, score_row: EngagementScore) -> bool:
        """Insert or update a score row. Records a history entry if the
        tier changed since the last upsert. Returns True if the tier
        transitioned."""
        assert self._conn is not None

        prior = await self.get(score_row.email)
        await self._conn.execute(
            "INSERT INTO engagement_score "
            "(email, score, tier, opens_30d, clicks_30d, last_open_at, "
            " last_click_at, days_since_last_event, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "  score = excluded.score, "
            "  tier = excluded.tier, "
            "  opens_30d = excluded.opens_30d, "
            "  clicks_30d = excluded.clicks_30d, "
            "  last_open_at = excluded.last_open_at, "
            "  last_click_at = excluded.last_click_at, "
            "  days_since_last_event = excluded.days_since_last_event, "
            "  updated_at = excluded.updated_at",
            (
                score_row.email,
                score_row.score,
                score_row.tier,
                score_row.opens_30d,
                score_row.clicks_30d,
                score_row.last_open_at,
                score_row.last_click_at,
                score_row.days_since_last_event,
                score_row.updated_at,
            ),
        )

        tier_changed = (prior is None) or (prior.tier != score_row.tier)
        if tier_changed:
            await self._conn.execute(
                "INSERT INTO engagement_score_history "
                "(email, score, tier, recorded_at) VALUES (?, ?, ?, ?)",
                (
                    score_row.email,
                    score_row.score,
                    score_row.tier,
                    score_row.updated_at,
                ),
            )

        await self._conn.commit()
        return tier_changed

    # ---- recompute cron ----------------------------------------------

    async def recompute_all(self, *, now: int | None = None) -> dict:
        """Rebuild scores for every recipient with at least one event in
        the scoring window. Returns a summary dict for cron logging.

        Idempotent. Safe to run hourly even though the cron schedule will
        be nightly. Recipients with no recent events fall to cold and
        their row gets updated with zeroed counts.
        """
        assert self._conn is not None
        now_ts = int(now if now is not None else time.time())
        window_start = now_ts - SCORING_WINDOW_SECONDS

        # Pull one row per recipient summarising their last 30d of events.
        # The recipient_domain column would let us segment by inbox later,
        # but the score itself is per-email so we don't need it here.
        async with self._conn.execute(
            "SELECT LOWER(recipient) AS email, "
            "       SUM(CASE WHEN event_type='opened'  THEN 1 ELSE 0 END), "
            "       SUM(CASE WHEN event_type='clicked' THEN 1 ELSE 0 END), "
            "       MAX(CASE WHEN event_type='opened'  THEN event_at END), "
            "       MAX(CASE WHEN event_type='clicked' THEN event_at END), "
            "       MAX(event_at) "
            "FROM email_events "
            "WHERE event_at >= ? "
            "GROUP BY LOWER(recipient)",
            (window_start,),
        ) as cursor:
            rows = await cursor.fetchall()

        updated = 0
        transitions = 0
        tier_dist = {TIER_HOT: 0, TIER_WARM: 0, TIER_COLD: 0}

        for row in rows:
            email = row[0]
            if not email:
                continue
            opens = int(row[1] or 0)
            clicks = int(row[2] or 0)
            last_open_at = int(row[3]) if row[3] is not None else None
            last_click_at = int(row[4]) if row[4] is not None else None
            last_any_at = int(row[5]) if row[5] is not None else None

            score = compute_score(
                opens=opens,
                clicks=clicks,
                last_open_at=last_open_at,
                last_click_at=last_click_at,
                now=now_ts,
            )
            tier = tier_for_score(score)
            tier_dist[tier] += 1

            days_since = (
                (now_ts - last_any_at) // (24 * 3600)
                if last_any_at is not None else None
            )

            row_out = EngagementScore(
                email=email,
                score=score,
                tier=tier,
                opens_30d=opens,
                clicks_30d=clicks,
                last_open_at=last_open_at,
                last_click_at=last_click_at,
                days_since_last_event=days_since,
                updated_at=now_ts,
            )
            transitioned = await self.upsert(row_out)
            updated += 1
            if transitioned:
                transitions += 1

        logger.info(
            "engagement_score recompute: updated=%d transitions=%d "
            "tier_distribution=%s",
            updated, transitions, tier_dist,
        )
        return {
            "updated": updated,
            "transitions": transitions,
            "tier_distribution": tier_dist,
            "computed_at": now_ts,
        }


# ---------------------------------------------------------------------------
# Pure scoring function (extracted so unit tests don't need a DB)
# ---------------------------------------------------------------------------


def compute_score(
    *,
    opens: int,
    clicks: int,
    last_open_at: int | None,
    last_click_at: int | None,
    now: int,
    replies: int = 0,
) -> int:
    """Pure deterministic scoring. The recompute cron and any future
    on-demand score endpoint share this function so the math can't drift
    between code paths.
    """
    score = (
        opens * OPEN_POINTS
        + clicks * CLICK_POINTS
        + replies * REPLY_POINTS
    )
    if (
        last_open_at is not None
        and (now - last_open_at) <= RECENT_OPEN_BONUS_WINDOW
    ):
        score += RECENT_OPEN_BONUS_POINTS
    if (
        last_click_at is not None
        and (now - last_click_at) <= RECENT_CLICK_BONUS_WINDOW
    ):
        score += RECENT_CLICK_BONUS_POINTS
    return int(score)
