"""SQLite persistence for the wallet scout + watcher.

Four tables in one new db (data/wallet_scout.db):

  wallet_metrics    one row per (address, snapshot_date): the nightly
                    leaderboard windows + trade-level verification stats +
                    the copyability score. History, not a snapshot, so
                    scoring/hysteresis can use trends.

  tracked_wallets   the promote/demote state machine. status is
                    'candidate' or 'tracked'; streak counters implement
                    hysteresis so one good/bad night never churns the set.

  wallet_positions  the watcher's last-seen open positions per wallet, so
                    a restart baselines from disk instead of re-proposing
                    everything already open.

  watcher_events    append-only audit log of detected deltas + actions.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS wallet_metrics (
  address           TEXT NOT NULL,
  snapshot_date     TEXT NOT NULL,
  account_value     REAL NOT NULL DEFAULT 0,
  day_pnl           REAL NOT NULL DEFAULT 0,
  week_pnl          REAL NOT NULL DEFAULT 0,
  month_pnl         REAL NOT NULL DEFAULT 0,
  alltime_pnl       REAL NOT NULL DEFAULT 0,
  month_roi         REAL NOT NULL DEFAULT 0,
  volume            REAL NOT NULL DEFAULT 0,
  fills_per_day     REAL NOT NULL DEFAULT 0,
  win_rate          REAL NOT NULL DEFAULT 0,
  profit_factor     REAL NOT NULL DEFAULT 0,
  max_drawdown_pct  REAL NOT NULL DEFAULT 0,
  median_hold_min   REAL NOT NULL DEFAULT 0,
  conviction_median REAL NOT NULL DEFAULT 0,
  blofin_coverage   REAL NOT NULL DEFAULT 0,
  top_trade_share   REAL NOT NULL DEFAULT 0,
  hours_since_fill  REAL NOT NULL DEFAULT 0,
  score             REAL NOT NULL DEFAULT 0,
  created_at        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (address, snapshot_date)
);

CREATE TABLE IF NOT EXISTS tracked_wallets (
  address       TEXT PRIMARY KEY,
  status        TEXT NOT NULL DEFAULT 'candidate',
  score         REAL NOT NULL DEFAULT 0,
  streak_above  INTEGER NOT NULL DEFAULT 0,
  streak_below  INTEGER NOT NULL DEFAULT 0,
  is_scalper    INTEGER NOT NULL DEFAULT 0,
  promoted_at   INTEGER,
  demoted_at    INTEGER,
  updated_at    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS wallet_positions (
  address    TEXT NOT NULL,
  coin       TEXT NOT NULL,
  szi        REAL NOT NULL,
  entry_px   REAL NOT NULL DEFAULT 0,
  leverage   REAL NOT NULL DEFAULT 0,
  notional   REAL NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (address, coin)
);

CREATE TABLE IF NOT EXISTS wallet_baselines (
  address     TEXT PRIMARY KEY,
  baselined_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watcher_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  address    TEXT NOT NULL,
  coin       TEXT NOT NULL,
  kind       TEXT NOT NULL,
  detail     TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass
class WalletMetrics:
    """One nightly measurement of one wallet."""

    address: str
    snapshot_date: str                 # YYYY-MM-DD (UTC)
    account_value: float = 0.0
    day_pnl: float = 0.0
    week_pnl: float = 0.0
    month_pnl: float = 0.0
    alltime_pnl: float = 0.0
    month_roi: float = 0.0
    volume: float = 0.0
    fills_per_day: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    median_hold_min: float = 0.0
    conviction_median: float = 0.0
    blofin_coverage: float = 0.0       # fraction of traded coins listed on Blofin
    top_trade_share: float = 0.0       # best episode pnl / total positive pnl
    hours_since_fill: float = 0.0
    score: float = 0.0


@dataclass
class TrackedWallet:
    address: str
    status: str = "candidate"          # 'candidate' | 'tracked'
    score: float = 0.0
    streak_above: int = 0
    streak_below: int = 0
    is_scalper: bool = False
    promoted_at: int | None = None
    demoted_at: int | None = None


@dataclass
class StoredPosition:
    coin: str
    szi: float
    entry_px: float = 0.0
    leverage: float = 0.0
    notional: float = 0.0


class WalletMetricsDB:
    def __init__(self, *, db_path: str):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_DDL)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        assert self._db is not None, "call open() first"
        return self._db

    # ---- metrics history --------------------------------------------------

    async def upsert_metrics(self, m: WalletMetrics) -> None:
        await self._conn.execute(
            """
            INSERT INTO wallet_metrics (
              address, snapshot_date, account_value, day_pnl, week_pnl,
              month_pnl, alltime_pnl, month_roi, volume, fills_per_day,
              win_rate, profit_factor, max_drawdown_pct, median_hold_min,
              conviction_median, blofin_coverage, top_trade_share,
              hours_since_fill, score, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(address, snapshot_date) DO UPDATE SET
              account_value=excluded.account_value,
              day_pnl=excluded.day_pnl, week_pnl=excluded.week_pnl,
              month_pnl=excluded.month_pnl, alltime_pnl=excluded.alltime_pnl,
              month_roi=excluded.month_roi, volume=excluded.volume,
              fills_per_day=excluded.fills_per_day,
              win_rate=excluded.win_rate, profit_factor=excluded.profit_factor,
              max_drawdown_pct=excluded.max_drawdown_pct,
              median_hold_min=excluded.median_hold_min,
              conviction_median=excluded.conviction_median,
              blofin_coverage=excluded.blofin_coverage,
              top_trade_share=excluded.top_trade_share,
              hours_since_fill=excluded.hours_since_fill,
              score=excluded.score
            """,
            (
                m.address, m.snapshot_date, m.account_value, m.day_pnl,
                m.week_pnl, m.month_pnl, m.alltime_pnl, m.month_roi,
                m.volume, m.fills_per_day, m.win_rate, m.profit_factor,
                m.max_drawdown_pct, m.median_hold_min, m.conviction_median,
                m.blofin_coverage, m.top_trade_share, m.hours_since_fill,
                m.score, int(time.time()),
            ),
        )
        await self._conn.commit()

    async def recent_scores(self, address: str, limit: int = 7) -> list[float]:
        """Most-recent-first scores for trend-aware decisions."""
        cur = await self._conn.execute(
            "SELECT score FROM wallet_metrics WHERE address=? "
            "ORDER BY snapshot_date DESC LIMIT ?",
            (address, limit),
        )
        rows = await cur.fetchall()
        return [float(r["score"]) for r in rows]

    async def latest_metrics(self, address: str) -> WalletMetrics | None:
        cur = await self._conn.execute(
            "SELECT * FROM wallet_metrics WHERE address=? "
            "ORDER BY snapshot_date DESC LIMIT 1",
            (address,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return WalletMetrics(
            address=row["address"], snapshot_date=row["snapshot_date"],
            account_value=row["account_value"], day_pnl=row["day_pnl"],
            week_pnl=row["week_pnl"], month_pnl=row["month_pnl"],
            alltime_pnl=row["alltime_pnl"], month_roi=row["month_roi"],
            volume=row["volume"], fills_per_day=row["fills_per_day"],
            win_rate=row["win_rate"], profit_factor=row["profit_factor"],
            max_drawdown_pct=row["max_drawdown_pct"],
            median_hold_min=row["median_hold_min"],
            conviction_median=row["conviction_median"],
            blofin_coverage=row["blofin_coverage"],
            top_trade_share=row["top_trade_share"],
            hours_since_fill=row["hours_since_fill"], score=row["score"],
        )

    # ---- tracked set ------------------------------------------------------

    async def get_tracked_wallet(self, address: str) -> TrackedWallet | None:
        cur = await self._conn.execute(
            "SELECT * FROM tracked_wallets WHERE address=?", (address,),
        )
        row = await cur.fetchone()
        return self._tracked_from_row(row) if row is not None else None

    async def list_wallets(self, status: str | None = None) -> list[TrackedWallet]:
        if status is None:
            cur = await self._conn.execute(
                "SELECT * FROM tracked_wallets ORDER BY score DESC",
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM tracked_wallets WHERE status=? ORDER BY score DESC",
                (status,),
            )
        rows = await cur.fetchall()
        return [self._tracked_from_row(r) for r in rows]

    @staticmethod
    def _tracked_from_row(row) -> TrackedWallet:
        return TrackedWallet(
            address=row["address"], status=row["status"],
            score=row["score"], streak_above=row["streak_above"],
            streak_below=row["streak_below"],
            is_scalper=bool(row["is_scalper"]),
            promoted_at=row["promoted_at"], demoted_at=row["demoted_at"],
        )

    async def save_tracked_wallet(self, w: TrackedWallet) -> None:
        await self._conn.execute(
            """
            INSERT INTO tracked_wallets (
              address, status, score, streak_above, streak_below,
              is_scalper, promoted_at, demoted_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(address) DO UPDATE SET
              status=excluded.status, score=excluded.score,
              streak_above=excluded.streak_above,
              streak_below=excluded.streak_below,
              is_scalper=excluded.is_scalper,
              promoted_at=excluded.promoted_at,
              demoted_at=excluded.demoted_at,
              updated_at=excluded.updated_at
            """,
            (
                w.address, w.status, w.score, w.streak_above, w.streak_below,
                1 if w.is_scalper else 0, w.promoted_at, w.demoted_at,
                int(time.time()),
            ),
        )
        await self._conn.commit()

    async def remove_wallet(self, address: str) -> None:
        await self._conn.execute(
            "DELETE FROM tracked_wallets WHERE address=?", (address,),
        )
        await self._conn.execute(
            "DELETE FROM wallet_positions WHERE address=?", (address,),
        )
        await self._conn.execute(
            "DELETE FROM wallet_baselines WHERE address=?", (address,),
        )
        await self._conn.commit()

    # ---- watcher position baseline ----------------------------------------

    async def is_baselined(self, address: str) -> bool:
        cur = await self._conn.execute(
            "SELECT 1 FROM wallet_baselines WHERE address=?", (address,),
        )
        return await cur.fetchone() is not None

    async def get_positions(self, address: str) -> dict[str, StoredPosition]:
        cur = await self._conn.execute(
            "SELECT * FROM wallet_positions WHERE address=?", (address,),
        )
        rows = await cur.fetchall()
        return {
            r["coin"]: StoredPosition(
                coin=r["coin"], szi=r["szi"], entry_px=r["entry_px"],
                leverage=r["leverage"], notional=r["notional"],
            )
            for r in rows
        }

    async def replace_positions(
        self, address: str, positions: dict[str, StoredPosition],
    ) -> None:
        now = int(time.time())
        await self._conn.execute(
            "DELETE FROM wallet_positions WHERE address=?", (address,),
        )
        for pos in positions.values():
            await self._conn.execute(
                "INSERT INTO wallet_positions "
                "(address, coin, szi, entry_px, leverage, notional, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    address, pos.coin, pos.szi, pos.entry_px,
                    pos.leverage, pos.notional, now,
                ),
            )
        await self._conn.execute(
            "INSERT INTO wallet_baselines (address, baselined_at) VALUES (?,?) "
            "ON CONFLICT(address) DO UPDATE SET baselined_at=excluded.baselined_at",
            (address, now),
        )
        await self._conn.commit()

    # ---- audit log ---------------------------------------------------------

    async def log_event(
        self, address: str, coin: str, kind: str, detail: dict | str = "",
    ) -> None:
        text = detail if isinstance(detail, str) else json.dumps(detail)
        await self._conn.execute(
            "INSERT INTO watcher_events (address, coin, kind, detail, created_at) "
            "VALUES (?,?,?,?,?)",
            (address, coin, kind, text, int(time.time())),
        )
        await self._conn.commit()
