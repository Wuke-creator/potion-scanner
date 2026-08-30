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

# "argument not supplied", so a caller that says nothing about the stop is not confused with
# one saying the stop is gone. See mark_copy_trade_filled.
_KEEP = object()

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

CREATE TABLE IF NOT EXISTS copy_trades (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  leader_address   TEXT NOT NULL,
  coin             TEXT NOT NULL,
  inst_id          TEXT NOT NULL DEFAULT '',
  telegram_user_id INTEGER NOT NULL,
  side             TEXT NOT NULL,
  proposal_id      INTEGER NOT NULL,
  proposed_at      INTEGER NOT NULL,
  proposal_price   REAL,
  atr_at_proposal  REAL,
  confirmed_at     INTEGER,
  order_ref        TEXT NOT NULL DEFAULT '',
  entry_price      REAL,
  size_base        REAL,
  leverage         INTEGER,
  status           TEXT NOT NULL DEFAULT 'proposed',
  closed_at        INTEGER,
  realized_pnl     REAL,
  close_reason     TEXT NOT NULL DEFAULT '',
  stop_price       REAL
);
CREATE INDEX IF NOT EXISTS idx_copy_trades_leader
  ON copy_trades(leader_address, status);
CREATE INDEX IF NOT EXISTS idx_copy_trades_proposal
  ON copy_trades(proposal_id, telegram_user_id);
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
    # latest /backtest verdict for this wallet (scoring v2 latency gate)
    bt_latency_ratio: float | None = None
    bt_copier_net: float | None = None
    bt_at: int = 0


@dataclass
class StoredPosition:
    coin: str
    szi: float
    entry_px: float = 0.0
    leverage: float = 0.0
    notional: float = 0.0


@dataclass
class CopyTrade:
    """One proposed/placed copy of a tracked wallet's trade.

    Lifecycle: proposed -> filled -> closed
                        -> expired | cancelled_deviation | cancelled
               filled  -> closed_unreconciled (position gone without a
                          recorded close; excluded from leader-stop sums)
    """

    id: int
    leader_address: str
    coin: str
    inst_id: str
    telegram_user_id: int
    side: str
    proposal_id: int
    proposed_at: int
    proposal_price: float | None = None
    atr_at_proposal: float | None = None
    confirmed_at: int | None = None
    order_ref: str = ""
    entry_price: float | None = None
    size_base: float | None = None
    leverage: int | None = None
    status: str = "proposed"
    closed_at: int | None = None
    realized_pnl: float | None = None
    close_reason: str = ""
    stop_price: float | None = None


class WalletMetricsDB:
    def __init__(self, *, db_path: str):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_DDL)
        # additive migrations for dbs created before these columns existed
        await self._ensure_column("copy_trades", "stop_price", "REAL")
        await self._ensure_column("tracked_wallets", "bt_latency_ratio", "REAL")
        await self._ensure_column("tracked_wallets", "bt_copier_net", "REAL")
        await self._ensure_column(
            "tracked_wallets", "bt_at", "INTEGER NOT NULL DEFAULT 0",
        )
        await self._db.commit()

    async def _ensure_column(self, table: str, column: str, decl: str) -> None:
        assert self._db is not None
        cur = await self._db.execute(f"PRAGMA table_info({table})")
        cols = {r["name"] for r in await cur.fetchall()}
        if column not in cols:
            await self._db.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {decl}",
            )

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
        keys = row.keys()
        return TrackedWallet(
            address=row["address"], status=row["status"],
            score=row["score"], streak_above=row["streak_above"],
            streak_below=row["streak_below"],
            is_scalper=bool(row["is_scalper"]),
            promoted_at=row["promoted_at"], demoted_at=row["demoted_at"],
            bt_latency_ratio=(
                row["bt_latency_ratio"] if "bt_latency_ratio" in keys else None
            ),
            bt_copier_net=(
                row["bt_copier_net"] if "bt_copier_net" in keys else None
            ),
            bt_at=int(row["bt_at"] or 0) if "bt_at" in keys else 0,
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

    async def record_backtest_fitness(
        self, address: str, *, latency_ratio: float | None,
        copier_net: float | None,
    ) -> None:
        """Attach the latest /backtest verdict to a wallet (creating a
        candidate row if the scout has never seen it)."""
        existing = await self.get_tracked_wallet(address)
        if existing is None:
            await self.save_tracked_wallet(TrackedWallet(address=address))
        await self._conn.execute(
            "UPDATE tracked_wallets SET bt_latency_ratio=?, bt_copier_net=?, "
            "bt_at=? WHERE address=?",
            (latency_ratio, copier_net, int(time.time()), address),
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

    # ---- copy-trade attribution ---------------------------------------------

    @staticmethod
    def _copy_trade_from_row(row) -> CopyTrade:
        return CopyTrade(**{k: row[k] for k in row.keys()})

    async def insert_copy_trade(
        self, *, leader_address: str, coin: str, inst_id: str,
        telegram_user_id: int, side: str, proposal_id: int,
        proposal_price: float | None, atr_at_proposal: float | None,
        stop_price: float | None = None,
    ) -> int:
        cur = await self._conn.execute(
            "INSERT INTO copy_trades (leader_address, coin, inst_id, "
            "telegram_user_id, side, proposal_id, proposed_at, "
            "proposal_price, atr_at_proposal, stop_price) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                leader_address.lower(), coin, inst_id, telegram_user_id,
                side, proposal_id, int(time.time()), proposal_price,
                atr_at_proposal, stop_price,
            ),
        )
        await self._conn.commit()
        return int(cur.lastrowid)

    async def get_copy_trade(
        self, proposal_id: int, telegram_user_id: int,
    ) -> CopyTrade | None:
        cur = await self._conn.execute(
            "SELECT * FROM copy_trades WHERE proposal_id=? AND "
            "telegram_user_id=? ORDER BY id DESC LIMIT 1",
            (proposal_id, telegram_user_id),
        )
        row = await cur.fetchone()
        return self._copy_trade_from_row(row) if row else None

    async def set_copy_trade_status(
        self, proposal_id: int, telegram_user_id: int, status: str,
    ) -> None:
        await self._conn.execute(
            "UPDATE copy_trades SET status=? WHERE proposal_id=? AND "
            "telegram_user_id=? AND status='proposed'",
            (status, proposal_id, telegram_user_id),
        )
        await self._conn.commit()

    async def mark_copy_trade_filled(
        self, proposal_id: int, telegram_user_id: int, *,
        order_ref: str, size_base: float | None, leverage: int | None,
        entry_price: float | None = None,
        stop_price: float | None | object = _KEEP,
    ) -> None:
        """Mark a proposal filled.

        `stop_price` is the stop that reached the venue. For a wallet copy that
        is re-anchored to our fill and so differs from the proposal-time level,
        and it is None when the venue REJECTED the stop: open_copy_heat_usd
        measures risk as abs(entry - stop), so booking a rejected stop as if it
        were live would count an unprotected position as bounded risk.

        Omitting the argument leaves the column untouched (the proposal-time
        value stands). Passing None clears it on purpose. Those are different
        instructions and must not collapse into one.
        """
        set_stop = stop_price is not _KEEP
        sql = ("UPDATE copy_trades SET status='filled', confirmed_at=?, "
               "order_ref=?, size_base=?, leverage=?, entry_price=?"
               + (", stop_price=? " if set_stop else " ")
               + "WHERE proposal_id=? AND telegram_user_id=? AND status='proposed'")
        params = [int(time.time()), order_ref, size_base, leverage, entry_price]
        if set_stop:
            params.append(stop_price)
        params += [proposal_id, telegram_user_id]
        await self._conn.execute(sql, tuple(params))
        await self._conn.commit()

    async def open_copy_trades(
        self, leader_address: str, coin: str | None = None,
    ) -> list[CopyTrade]:
        if coin is None:
            cur = await self._conn.execute(
                "SELECT * FROM copy_trades WHERE leader_address=? AND "
                "status='filled' ORDER BY id",
                (leader_address.lower(),),
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM copy_trades WHERE leader_address=? AND "
                "coin=? AND status='filled' ORDER BY id",
                (leader_address.lower(), coin),
            )
        return [self._copy_trade_from_row(r) for r in await cur.fetchall()]

    async def close_copy_trade(
        self, trade_id: int, *, close_reason: str,
        realized_pnl: float | None, status: str = "closed",
    ) -> None:
        await self._conn.execute(
            "UPDATE copy_trades SET status=?, closed_at=?, realized_pnl=?, "
            "close_reason=? WHERE id=?",
            (status, int(time.time()), realized_pnl, close_reason, trade_id),
        )
        await self._conn.commit()

    async def realized_pnl_since(self, since_ts: int) -> float:
        """Sleeve-wide recorded realized pnl since a timestamp (daily
        circuit breaker input). Unreconciled NULL-pnl rows excluded."""
        cur = await self._conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS s FROM copy_trades "
            "WHERE status='closed' AND closed_at>=? AND realized_pnl IS NOT NULL",
            (since_ts,),
        )
        row = await cur.fetchone()
        return float(row["s"] if row else 0.0)

    async def open_copy_heat_usd(self) -> float:
        """Total open entry-to-stop risk across ALL filled copies, $."""
        cur = await self._conn.execute(
            "SELECT entry_price, proposal_price, stop_price, size_base "
            "FROM copy_trades WHERE status='filled'",
        )
        heat = 0.0
        for r in await cur.fetchall():
            entry = r["entry_price"] or r["proposal_price"]
            stop = r["stop_price"]
            size = r["size_base"]
            if entry and stop and size:
                heat += abs(float(entry) - float(stop)) * float(size)
        return heat

    async def open_same_direction_count(self, side: str) -> int:
        cur = await self._conn.execute(
            "SELECT COUNT(*) AS n FROM copy_trades WHERE status='filled' "
            "AND side=?",
            (side,),
        )
        row = await cur.fetchone()
        return int(row["n"] if row else 0)

    async def recent_proposal_sizes(
        self, leader_address: str, limit: int = 10,
    ) -> list[float]:
        """size_pct of the leader's last N proposals (volume clamp input)."""
        cur = await self._conn.execute(
            "SELECT detail FROM watcher_events WHERE address=? AND "
            "kind='proposed' ORDER BY id DESC LIMIT ?",
            (leader_address, limit),
        )
        out: list[float] = []
        for r in await cur.fetchall():
            try:
                d = json.loads(r["detail"])
                v = float(d.get("size_pct", 0.0))
                if v > 0:
                    out.append(v)
            except (ValueError, TypeError):
                continue
        return out

    async def leaders_with_open_copies(self) -> list[str]:
        """Leaders we still hold filled copies of (wind-down mirroring
        continues for these even after demotion)."""
        cur = await self._conn.execute(
            "SELECT DISTINCT leader_address FROM copy_trades "
            "WHERE status='filled'",
        )
        return [r["leader_address"] for r in await cur.fetchall()]

    async def leader_realized_pnl(
        self, leader_address: str, *, since_ts: int = 0,
    ) -> float:
        """Sum of recorded realized pnl for closed copies of this leader.
        closed_unreconciled rows (NULL pnl) are deliberately excluded."""
        cur = await self._conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS s FROM copy_trades "
            "WHERE leader_address=? AND status='closed' AND closed_at>=? "
            "AND realized_pnl IS NOT NULL",
            (leader_address.lower(), since_ts),
        )
        row = await cur.fetchone()
        return float(row["s"] if row else 0.0)

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
