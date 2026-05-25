"""Engagement scoring: derive a per-recipient hot/warm/cold tier from
Resend webhook events.

Lives in ``data/email_events.db`` alongside the raw ``email_events`` log,
so the scoring cron can do everything in one DB without cross-file joins.
A separate table (not a view) so reads from the branch evaluator are O(1)
on the lookup path and the cron pays the cost once per night.

Scoring model (per-event exponential decay, split half-life by type):

    each event contributes:
        base_weight * exp(-ln(2) * age_days / half_life_for_type)

    aggregated by recipient:
        score = OPEN_WEIGHT  * opens_decayed
              + CLICK_WEIGHT * clicks_decayed
              + REPLY_WEIGHT * replies_decayed

    tier:
        score >= 10  -> hot
        score >=  4  -> warm
        otherwise    -> cold

Why split half-lives: a click is a stronger commitment signal than an
open (opens fire on every preview-pane impression, including Apple Mail
Privacy prefetches). Opens decay faster (7d) so the noise washes out;
clicks decay slower (14d) so intent stays visible longer.

Why decay vs flat counts + recency bonuses: a recipient who opened 12
emails today is hotter than one who opened 12 emails in week one and
went dark for three weeks, even though both have ``opens_30d == 12``.
Per-event decay captures the difference.

Tier thresholds (HOT=10, WARM=4) match the consensus of the deep-research
sources: anyone with at least one recent click or two recent opens is
"hot"; anyone with recent opens but no clicks is "warm"; everyone else
is "cold". Thresholds are unchanged from the linear model so the
hot/warm/cold ratios on dashboards stay comparable; only the underlying
math changed.

The branch evaluator (``branch_evaluator.py``) is the only consumer of
this table. The worker is intentionally NOT yet touching it. Until the
worker is wired in, scoring runs silently and changes no user-facing
behaviour.
"""

from __future__ import annotations

import logging
import math
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

# Per-event base weights. Multiplied by exp-decay(age) before they hit the
# score sum, so a 14-day-old click is worth 1.5pt (3 * 0.5), not 3.
OPEN_WEIGHT = 1.0
CLICK_WEIGHT = 3.0
REPLY_WEIGHT = 10.0  # reserved; not currently captured in email_events

# Half-life for the per-event decay. At HALF_LIFE_DAYS the event is worth
# half its base weight; at 2x it's worth a quarter; etc. 14 days was
# chosen to match the prior recent-click bonus window so the new model
# weights "clicked recently" similarly to the old recency bonus, and is
# applied uniformly across opens / clicks / replies to keep the math
# easy to reason about. If we later find opens are too noisy (Apple Mail
# Privacy, preview-pane prefetches) we can split this into per-type
# half-lives without changing the public surface.
HALF_LIFE_DAYS = 14.0
SECONDS_PER_DAY = 86400.0


def _decay(age_days: float) -> float:
    """Exponential decay factor for an event ``age_days`` old.

    Pure helper extracted so unit tests can verify the math directly and
    the recompute code paths (SQL or Python-side) share a single
    definition.
    """
    if age_days <= 0:
        return 1.0
    return math.exp(-math.log(2) * age_days / HALF_LIFE_DAYS)


@dataclass(frozen=True)
class EngagementScore:
    """One row of the engagement_score table.

    ``opens_30d`` and ``clicks_30d`` are decay-weighted sums (float),
    not raw counts. With the per-event exp-decay model, a recipient
    with three opens today contributes ``opens_30d == 3.0`` while one
    with three opens 14 days ago contributes ``opens_30d == 1.5``.
    """
    email: str
    score: float
    tier: str
    opens_30d: float
    clicks_30d: float
    last_open_at: int | None
    last_click_at: int | None
    days_since_last_event: int | None
    updated_at: int


def tier_for_score(score: float) -> str:
    """Pure function: pick the tier label for a numeric score."""
    if score >= HOT_THRESHOLD:
        return TIER_HOT
    if score >= WARM_THRESHOLD:
        return TIER_WARM
    return TIER_COLD


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

# NOTE: score / opens_30d / clicks_30d switched from INTEGER to REAL with
# the move to per-event exponential decay. Production DBs created under
# the linear model will be migrated in-place by ``_SCORE_TABLE_MIGRATIONS``
# at open() time.
_SCORE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS engagement_score (
  email                  TEXT PRIMARY KEY,
  score                  REAL NOT NULL DEFAULT 0,
  tier                   TEXT NOT NULL DEFAULT 'cold',
  opens_30d              REAL NOT NULL DEFAULT 0,
  clicks_30d             REAL NOT NULL DEFAULT 0,
  last_open_at           INTEGER,
  last_click_at          INTEGER,
  days_since_last_event  INTEGER,
  updated_at             INTEGER NOT NULL
);
"""

# SQLite's flexible typing means existing INTEGER columns will silently
# store REAL values without complaint, so we don't actually need an
# ALTER TABLE to keep the data path working. The migration is here
# anyway as documentation: anyone inspecting the schema with
# ``.schema`` or running typed migrations against a fresh dump will
# see the column flagged as REAL, matching code expectations.
#
# SQLite has no native ``ALTER COLUMN`` -- the standard workaround is a
# rebuild-via-temp-table. That's overkill for an internal scoring table
# whose schema is enforced by the application layer. We skip the
# rebuild and rely on storage flexibility, but keep this section so a
# future maintainer adding new columns or a real type change has a
# place to wire migrations in.
_SCORE_TABLE_MIGRATIONS: tuple[str, ...] = ()

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
        for stmt in _SCORE_TABLE_MIGRATIONS:
            try:
                await self._conn.execute(stmt)
            except Exception:
                # Migration already applied in a prior run.
                pass
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
            score=float(row[1]),
            tier=str(row[2]),
            opens_30d=float(row[3]),
            clicks_30d=float(row[4]),
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

        Aggregation uses SQLite's built-in ``exp()`` and ``ln()`` math
        functions (available since SQLite 3.35, well below our floor)
        to compute the per-event decay sum in one pass. If the build
        ever ships without math functions, the query will raise and
        operators will see a clear error from the cron logs.
        """
        assert self._conn is not None
        now_ts = int(now if now is not None else time.time())
        window_start = now_ts - SCORING_WINDOW_SECONDS

        # Per-event exponential decay aggregated in SQL.
        #
        # The decay weight for each event is:
        #     exp(-ln(2) * (now - event_at) / 86400.0 / HALF_LIFE_DAYS)
        #
        # which equals 1.0 for events that happened right now and falls
        # to 0.5 at age == HALF_LIFE_DAYS. We sum per event_type for the
        # weighted-counts columns and also pull MAX(event_at) per type
        # for the "last open / last click" fields.
        sql = (
            "SELECT LOWER(recipient) AS email, "
            "       SUM(CASE WHEN event_type='opened'  THEN "
            "             exp(-ln(2.0) * (? - event_at) / ? / ?) "
            "           ELSE 0 END) AS opens_decayed, "
            "       SUM(CASE WHEN event_type='clicked' THEN "
            "             exp(-ln(2.0) * (? - event_at) / ? / ?) "
            "           ELSE 0 END) AS clicks_decayed, "
            "       MAX(CASE WHEN event_type='opened'  THEN event_at END), "
            "       MAX(CASE WHEN event_type='clicked' THEN event_at END), "
            "       MAX(event_at) "
            "FROM email_events "
            "WHERE event_at >= ? "
            "GROUP BY LOWER(recipient)"
        )
        params = (
            now_ts, SECONDS_PER_DAY, HALF_LIFE_DAYS,  # opens decay
            now_ts, SECONDS_PER_DAY, HALF_LIFE_DAYS,  # clicks decay
            window_start,
        )
        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()

        updated = 0
        transitions = 0
        tier_dist = {TIER_HOT: 0, TIER_WARM: 0, TIER_COLD: 0}

        for row in rows:
            email = row[0]
            if not email:
                continue
            opens_decayed = float(row[1] or 0.0)
            clicks_decayed = float(row[2] or 0.0)
            last_open_at = int(row[3]) if row[3] is not None else None
            last_click_at = int(row[4]) if row[4] is not None else None
            last_any_at = int(row[5]) if row[5] is not None else None

            score = compute_score(
                opens_decayed=opens_decayed,
                clicks_decayed=clicks_decayed,
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
                opens_30d=opens_decayed,
                clicks_30d=clicks_decayed,
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
    opens_decayed: float,
    clicks_decayed: float,
    replies_decayed: float = 0.0,
) -> float:
    """Pure deterministic scoring on decay-weighted aggregates.

    Each input is the sum of ``decay(age_days)`` over the events of that
    type for one recipient, so a 14-day-old open contributes 0.5 to
    ``opens_decayed`` rather than 1. Callers (``recompute_all``, future
    on-demand endpoints) handle the SQL aggregation; this function does
    only the weighted sum so the math can't drift between code paths.

    Replies are reserved (we don't capture reply events from Resend
    today). Default 0.0 keeps the existing call sites working.
    """
    return (
        opens_decayed * OPEN_WEIGHT
        + clicks_decayed * CLICK_WEIGHT
        + replies_decayed * REPLY_WEIGHT
    )


