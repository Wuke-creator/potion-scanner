"""Read the Potion Scanner analytics DB for email template placeholders.

Everything here is a pure read against the same ``data/analytics.db``
that the TG bot writes to. The email bot never mutates analytics.

Produces a plain dict ready to substitute into template placeholders:

    {
      "calls_7d_total": 24,
      "wins_7d_over_50pct": 3,
      "top_calls_7d": [
          {"pair": "ETH/USDT", "pnl_pct": 180.0, "days_ago": 2},
          {"pair": "PEPE/USDT", "pnl_pct": 480.0, "days_ago": 1},
      ],
      "top_call_7d_line": "+480% on PEPE/USDT",
      ...
    }
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass
class StatsBundle:
    """All the dynamic bits the email templates need."""

    calls_7d_total: int
    wins_7d_over_50pct: int
    top_call_7d: dict | None         # {pair, pnl_pct, days_ago} or None
    top_calls_7d: list[dict]         # up to 3 entries, sorted desc
    calls_30d_total: int
    top_call_30d: dict | None

    def as_dict(self) -> dict:
        return {
            "calls_7d_total": self.calls_7d_total,
            "wins_7d_over_50pct": self.wins_7d_over_50pct,
            "top_call_7d": self.top_call_7d,
            "top_calls_7d": self.top_calls_7d,
            "calls_30d_total": self.calls_30d_total,
            "top_call_30d": self.top_call_30d,
        }


_WIN_EVENT_TYPES = ("tp_hit", "all_tp_hit")


async def _count_signals_since(conn: aiosqlite.Connection, cutoff: int) -> int:
    async with conn.execute(
        "SELECT COUNT(*) FROM trades WHERE opened_at >= ?", (cutoff,),
    ) as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def _count_wins_over_pct_since(
    conn: aiosqlite.Connection, cutoff: int, min_pct: float,
) -> int:
    async with conn.execute(
        "SELECT COUNT(DISTINCT trade_id || ':' || channel_key) "
        "FROM trade_events "
        "WHERE recorded_at >= ? "
        "  AND event_type IN ('tp_hit', 'all_tp_hit') "
        "  AND pnl_pct >= ?",
        (cutoff, min_pct),
    ) as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def _top_calls_since(
    conn: aiosqlite.Connection, cutoff: int, limit: int,
) -> list[dict]:
    """Return top N calls by peak pnl_pct since cutoff."""
    async with conn.execute(
        "SELECT t.pair, MAX(e.pnl_pct) as max_pnl, MIN(t.opened_at) "
        "FROM trade_events e "
        "JOIN trades t ON t.trade_id = e.trade_id "
        "              AND t.channel_key = e.channel_key "
        "WHERE e.recorded_at >= ? "
        "  AND e.event_type IN ('tp_hit', 'all_tp_hit') "
        "  AND e.pnl_pct IS NOT NULL "
        "GROUP BY t.trade_id, t.channel_key "
        "ORDER BY max_pnl DESC "
        "LIMIT ?",
        (cutoff, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    now = int(time.time())
    out: list[dict] = []
    for r in rows:
        pair, pnl_pct, opened_at = r[0], r[1], r[2]
        out.append({
            "pair": pair,
            "pnl_pct": float(pnl_pct),
            "days_ago": max(0, (now - int(opened_at)) // 86400),
        })
    return out


async def gather_stats(analytics_db_path: str) -> StatsBundle:
    """Read the analytics DB and return a StatsBundle for template rendering."""
    now = int(time.time())
    cutoff_7d = now - 7 * 86400
    cutoff_30d = now - 30 * 86400

    conn = await aiosqlite.connect(analytics_db_path)
    try:
        calls_7d = await _count_signals_since(conn, cutoff_7d)
        calls_30d = await _count_signals_since(conn, cutoff_30d)
        wins_7d_50 = await _count_wins_over_pct_since(conn, cutoff_7d, 50.0)
        top_7d = await _top_calls_since(conn, cutoff_7d, 3)
        top_30d = await _top_calls_since(conn, cutoff_30d, 1)
    finally:
        await conn.close()

    return StatsBundle(
        calls_7d_total=calls_7d,
        wins_7d_over_50pct=wins_7d_50,
        top_call_7d=top_7d[0] if top_7d else None,
        top_calls_7d=top_7d,
        calls_30d_total=calls_30d,
        top_call_30d=top_30d[0] if top_30d else None,
    )
