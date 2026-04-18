"""SQLite analytics store for signals + lifecycle PnL events.

Tables:

  trades:
    (trade_id, channel_key) PRIMARY KEY. One row per SIGNAL_ALERT we saw.
    Holds the immutable trade parameters (pair, side, entry, leverage)
    plus opened_at timestamp.

  trade_events:
    Append-only log. One row per lifecycle event. Stores the pnl_pct
    value the source already computed (positive for TP hits, negative
    for stops).

The analytics DB is deliberately isolated from the verification DB. If a
reset is ever needed (schema change, bad data import), we can drop
analytics.db without touching user accounts.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass
class TopPnL:
    """A single trade's peak PnL result in a given timeframe."""

    trade_id: int
    channel_key: str
    pair: str
    side: str
    leverage: int
    pnl_pct: float
    event_type: str          # tp_hit, all_tp_hit
    recorded_at: int         # epoch when the TP hit was posted
    opened_at: int           # epoch when the signal was originally issued


@dataclass
class ChannelStats:
    """Per-channel aggregate for one timeframe."""

    channel_key: str
    signal_count: int
    top_pnl: TopPnL | None   # None if no PnL events recorded


@dataclass
class StatsWindow:
    """A full window's worth of stats, one entry per channel."""

    window_label: str        # "7d" or "30d"
    per_channel: dict[str, ChannelStats]


_TRADES_DDL = """
CREATE TABLE IF NOT EXISTS trades (
  trade_id     INTEGER NOT NULL,
  channel_key  TEXT NOT NULL,
  pair         TEXT NOT NULL,
  side         TEXT NOT NULL,
  entry        REAL NOT NULL,
  leverage     INTEGER NOT NULL,
  opened_at    INTEGER NOT NULL,
  PRIMARY KEY (trade_id, channel_key)
);
"""

_TRADES_OPENED_INDEX = """
CREATE INDEX IF NOT EXISTS idx_trades_opened
    ON trades (opened_at);
"""

_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS trade_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_id     INTEGER NOT NULL,
  channel_key  TEXT NOT NULL,
  event_type   TEXT NOT NULL,       -- tp_hit, all_tp_hit, breakeven, stop_hit, canceled, trade_closed
  tp_number    INTEGER,             -- nullable; set for tp_hit and breakeven
  pnl_pct      REAL,                -- leveraged %, positive for TP, negative for SL
  recorded_at  INTEGER NOT NULL
);
"""

_EVENTS_RECORDED_INDEX = """
CREATE INDEX IF NOT EXISTS idx_events_recorded
    ON trade_events (recorded_at);
"""

_EVENTS_TRADE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_events_trade
    ON trade_events (trade_id, channel_key);
"""


class AnalyticsDB:
    """Async SQLite wrapper for the analytics tables."""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(_TRADES_DDL)
        await self._conn.execute(_TRADES_OPENED_INDEX)
        await self._conn.execute(_EVENTS_DDL)
        await self._conn.execute(_EVENTS_RECORDED_INDEX)
        await self._conn.execute(_EVENTS_TRADE_INDEX)
        await self._conn.commit()
        logger.info("Analytics DB opened at %s", self._db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---- trades -------------------------------------------------------

    async def record_signal(
        self,
        trade_id: int,
        channel_key: str,
        pair: str,
        side: str,
        entry: float,
        leverage: int,
    ) -> None:
        """Record a new SIGNAL_ALERT. Idempotent on (trade_id, channel_key)."""
        assert self._conn is not None
        await self._conn.execute(
            "INSERT OR IGNORE INTO trades "
            "(trade_id, channel_key, pair, side, entry, leverage, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trade_id, channel_key, pair, side, entry, leverage, int(time.time())),
        )
        await self._conn.commit()

    async def record_event(
        self,
        trade_id: int,
        channel_key: str,
        event_type: str,
        tp_number: int | None = None,
        pnl_pct: float | None = None,
    ) -> None:
        """Append a lifecycle event for an existing trade."""
        assert self._conn is not None
        await self._conn.execute(
            "INSERT INTO trade_events "
            "(trade_id, channel_key, event_type, tp_number, pnl_pct, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (trade_id, channel_key, event_type, tp_number, pnl_pct, int(time.time())),
        )
        await self._conn.commit()

    # ---- queries ------------------------------------------------------

    async def count_signals_per_channel(
        self, since_epoch: int,
    ) -> dict[str, int]:
        """Return {channel_key: count} for signals opened since ``since_epoch``."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT channel_key, COUNT(*) FROM trades "
            "WHERE opened_at >= ? GROUP BY channel_key",
            (since_epoch,),
        ) as cursor:
            rows = await cursor.fetchall()
        return {r[0]: int(r[1]) for r in rows}

    async def top_pnl_per_channel(
        self, since_epoch: int,
    ) -> dict[str, TopPnL]:
        """Return {channel_key: TopPnL} for the biggest win in each channel.

        Only considers profit events (tp_hit, all_tp_hit). The signal's
        original ``opened_at`` is joined in so the caller can format the
        "called Xd ago" line.
        """
        assert self._conn is not None
        # SQLite trick: group by channel_key and pick the row with max pnl_pct
        # per group using a correlated subquery.
        async with self._conn.execute(
            "SELECT "
            "  t.trade_id, t.channel_key, t.pair, t.side, t.leverage, "
            "  e.pnl_pct, e.event_type, e.recorded_at, t.opened_at "
            "FROM trade_events e "
            "JOIN trades t ON t.trade_id = e.trade_id "
            "              AND t.channel_key = e.channel_key "
            "WHERE e.recorded_at >= ? "
            "  AND e.event_type IN ('tp_hit', 'all_tp_hit') "
            "  AND e.pnl_pct IS NOT NULL "
            "  AND e.pnl_pct = ("
            "    SELECT MAX(e2.pnl_pct) "
            "    FROM trade_events e2 "
            "    JOIN trades t2 ON t2.trade_id = e2.trade_id "
            "                   AND t2.channel_key = e2.channel_key "
            "    WHERE e2.recorded_at >= ? "
            "      AND e2.event_type IN ('tp_hit', 'all_tp_hit') "
            "      AND e2.pnl_pct IS NOT NULL "
            "      AND t2.channel_key = t.channel_key "
            "  ) "
            "GROUP BY t.channel_key",
            (since_epoch, since_epoch),
        ) as cursor:
            rows = await cursor.fetchall()
        out: dict[str, TopPnL] = {}
        for r in rows:
            out[r[1]] = TopPnL(
                trade_id=r[0],
                channel_key=r[1],
                pair=r[2],
                side=r[3],
                leverage=r[4],
                pnl_pct=r[5],
                event_type=r[6],
                recorded_at=r[7],
                opened_at=r[8],
            )
        return out

    async def stats_window(
        self, days: int, label: str, channel_keys: list[str],
    ) -> StatsWindow:
        """Compute per-channel stats for a rolling window.

        Always includes every channel_key in the result, even if the count
        is 0 (so the UI shows all channels consistently).
        """
        cutoff = int(time.time()) - days * 86400
        counts = await self.count_signals_per_channel(cutoff)
        tops = await self.top_pnl_per_channel(cutoff)
        per_channel: dict[str, ChannelStats] = {}
        for ch in channel_keys:
            per_channel[ch] = ChannelStats(
                channel_key=ch,
                signal_count=counts.get(ch, 0),
                top_pnl=tops.get(ch),
            )
        return StatsWindow(window_label=label, per_channel=per_channel)
