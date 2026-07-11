"""SQLite archive for backtesting: candles, funding, fills, leaderboard.

Lives in its OWN db file (data/backtest_cache.db), not wallet_scout.db:
the live watcher hits that db every 15 seconds and nightly archive writes
plus pruning must never contend with it.

Why this exists at all: Hyperliquid keeps only the most recent 5,000
candles per coin/interval (3.5 days of 1m) and publishes NO historical
leaderboard. Anything not archived nightly is permanently gone, so this
store is written to be boring, append-mostly, and pruned conservatively.

WAL journal + WITHOUT ROWID composite-PK tables (range scans on the PK are
the only access pattern) + incremental autovacuum after pruning.
"""

from __future__ import annotations

import json
import logging
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS candles (
  coin TEXT NOT NULL, interval TEXT NOT NULL, ts INTEGER NOT NULL,
  o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
  v REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (coin, interval, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS funding_rates (
  coin TEXT NOT NULL, ts INTEGER NOT NULL,
  rate REAL NOT NULL, premium REAL,
  PRIMARY KEY (coin, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS fills_cache (
  address TEXT NOT NULL, tid INTEGER NOT NULL,
  ts INTEGER NOT NULL, coin TEXT NOT NULL, raw TEXT NOT NULL,
  PRIMARY KEY (address, tid)
);
CREATE INDEX IF NOT EXISTS idx_fills_addr_ts ON fills_cache(address, ts);

CREATE TABLE IF NOT EXISTS fills_coverage (
  address TEXT NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
  complete INTEGER NOT NULL, fetched_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS leaderboard_snapshots (
  snapshot_date TEXT PRIMARY KEY, fetched_at INTEGER NOT NULL,
  n_rows INTEGER NOT NULL, payload BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS leaderboard_accounts (
  address TEXT NOT NULL, snapshot_date TEXT NOT NULL,
  account_value REAL NOT NULL,
  PRIMARY KEY (address, snapshot_date)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS wallet_state_snapshots (
  address TEXT NOT NULL, snapshot_date TEXT NOT NULL,
  account_value REAL NOT NULL, payload BLOB NOT NULL,
  PRIMARY KEY (address, snapshot_date)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS backtest_runs (
  run_id TEXT PRIMARY KEY, started_at INTEGER NOT NULL,
  spec_json TEXT NOT NULL DEFAULT '{}',
  params_json TEXT NOT NULL DEFAULT '{}',
  summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS backtest_trades (
  run_id TEXT NOT NULL, params_id TEXT NOT NULL, trade_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bt_trades_run ON backtest_trades(run_id);
"""

_INTERVAL_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


def interval_ms(interval: str) -> int:
    return _INTERVAL_MS.get(interval, 3_600_000)


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Candle:
    ts: int          # open time, ms
    o: float
    h: float
    l: float  # noqa: E741 - domain convention
    c: float
    v: float = 0.0


class BacktestStore:
    def __init__(self, *, db_path: str):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        # auto_vacuum must be set before tables exist to bite on a fresh db;
        # on an existing db it is a harmless no-op until a full VACUUM.
        await self._db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        await self._db.execute("PRAGMA journal_mode=WAL")
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

    # ---- candles -----------------------------------------------------------

    async def upsert_candles(
        self, coin: str, interval: str, rows: list[dict],
        *, drop_open_after_ms: int | None = None,
    ) -> int:
        """Store candleSnapshot rows. When drop_open_after_ms is given, the
        trailing in-progress candle (open time + interval > now) is skipped:
        an unfinished high/low poisons intrabar SL/TP resolution."""
        n = 0
        step = interval_ms(interval)
        for r in rows:
            try:
                ts = int(r["t"])
            except (KeyError, TypeError, ValueError):
                continue
            if drop_open_after_ms is not None and ts + step > drop_open_after_ms:
                continue
            await self._conn.execute(
                "INSERT INTO candles (coin, interval, ts, o, h, l, c, v) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(coin, interval, ts) DO UPDATE SET "
                "o=excluded.o, h=excluded.h, l=excluded.l, "
                "c=excluded.c, v=excluded.v",
                (
                    coin, interval, ts, _f(r.get("o")), _f(r.get("h")),
                    _f(r.get("l")), _f(r.get("c")), _f(r.get("v")),
                ),
            )
            n += 1
        await self._conn.commit()
        return n

    async def latest_candle_ts(self, coin: str, interval: str) -> int | None:
        cur = await self._conn.execute(
            "SELECT MAX(ts) AS m FROM candles WHERE coin=? AND interval=?",
            (coin, interval),
        )
        row = await cur.fetchone()
        return int(row["m"]) if row and row["m"] is not None else None

    async def get_candles(
        self, coin: str, interval: str, *, start_ms: int, end_ms: int,
    ) -> list[Candle]:
        cur = await self._conn.execute(
            "SELECT ts, o, h, l, c, v FROM candles "
            "WHERE coin=? AND interval=? AND ts>=? AND ts<=? ORDER BY ts",
            (coin, interval, int(start_ms), int(end_ms)),
        )
        rows = await cur.fetchall()
        return [
            Candle(ts=r["ts"], o=r["o"], h=r["h"], l=r["l"], c=r["c"], v=r["v"])
            for r in rows
        ]

    async def candle_coverage(
        self, coin: str, interval: str, *, start_ms: int, end_ms: int,
    ) -> float:
        """Fraction of expected candles present in [start, end]. Cheap
        honesty metric for the report's resolution flags."""
        step = interval_ms(interval)
        expected = max(1, (int(end_ms) - int(start_ms)) // step)
        cur = await self._conn.execute(
            "SELECT COUNT(*) AS n FROM candles "
            "WHERE coin=? AND interval=? AND ts>=? AND ts<=?",
            (coin, interval, int(start_ms), int(end_ms)),
        )
        row = await cur.fetchone()
        return min(1.0, (row["n"] if row else 0) / expected)

    # ---- funding -------------------------------------------------------------

    async def upsert_funding(self, coin: str, rows: list[dict]) -> int:
        n = 0
        for r in rows:
            try:
                ts = int(r["time"])
            except (KeyError, TypeError, ValueError):
                continue
            await self._conn.execute(
                "INSERT INTO funding_rates (coin, ts, rate, premium) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(coin, ts) DO UPDATE SET "
                "rate=excluded.rate, premium=excluded.premium",
                (coin, ts, _f(r.get("fundingRate")), _f(r.get("premium"))),
            )
            n += 1
        await self._conn.commit()
        return n

    async def latest_funding_ts(self, coin: str) -> int | None:
        cur = await self._conn.execute(
            "SELECT MAX(ts) AS m FROM funding_rates WHERE coin=?", (coin,),
        )
        row = await cur.fetchone()
        return int(row["m"]) if row and row["m"] is not None else None

    async def get_funding(
        self, coin: str, *, start_ms: int, end_ms: int,
    ) -> list[tuple[int, float]]:
        cur = await self._conn.execute(
            "SELECT ts, rate FROM funding_rates "
            "WHERE coin=? AND ts>=? AND ts<=? ORDER BY ts",
            (coin, int(start_ms), int(end_ms)),
        )
        return [(int(r["ts"]), float(r["rate"])) for r in await cur.fetchall()]

    # ---- fills ----------------------------------------------------------------

    async def upsert_fills(self, address: str, fills: list[dict]) -> int:
        n = 0
        for f in fills:
            tid = f.get("tid")
            ts = f.get("time")
            if tid is None or ts is None:
                continue
            await self._conn.execute(
                "INSERT OR REPLACE INTO fills_cache "
                "(address, tid, ts, coin, raw) VALUES (?,?,?,?,?)",
                (
                    address.lower(), int(tid), int(ts),
                    str(f.get("coin", "")), json.dumps(f),
                ),
            )
            n += 1
        await self._conn.commit()
        return n

    async def get_fills(
        self, address: str, *, start_ms: int, end_ms: int,
    ) -> list[dict]:
        cur = await self._conn.execute(
            "SELECT raw FROM fills_cache "
            "WHERE address=? AND ts>=? AND ts<=? ORDER BY ts",
            (address.lower(), int(start_ms), int(end_ms)),
        )
        return [json.loads(r["raw"]) for r in await cur.fetchall()]

    async def add_fills_coverage(
        self, address: str, *, start_ms: int, end_ms: int, complete: bool,
    ) -> None:
        await self._conn.execute(
            "INSERT INTO fills_coverage "
            "(address, start_ms, end_ms, complete, fetched_at) "
            "VALUES (?,?,?,?,?)",
            (
                address.lower(), int(start_ms), int(end_ms),
                1 if complete else 0, int(time.time()),
            ),
        )
        await self._conn.commit()

    async def fills_coverage(self, address: str) -> list[tuple[int, int, bool]]:
        cur = await self._conn.execute(
            "SELECT start_ms, end_ms, complete FROM fills_coverage "
            "WHERE address=? ORDER BY start_ms",
            (address.lower(),),
        )
        return [
            (int(r["start_ms"]), int(r["end_ms"]), bool(r["complete"]))
            for r in await cur.fetchall()
        ]

    # ---- leaderboard archive ---------------------------------------------------

    async def save_leaderboard_snapshot(
        self, snapshot_date: str, raw_body, *, n_rows: int,
    ) -> None:
        payload = zlib.compress(json.dumps(raw_body).encode("utf-8"), level=6)
        await self._conn.execute(
            "INSERT OR REPLACE INTO leaderboard_snapshots "
            "(snapshot_date, fetched_at, n_rows, payload) VALUES (?,?,?,?)",
            (snapshot_date, int(time.time()), int(n_rows), payload),
        )
        await self._conn.commit()

    async def has_leaderboard_snapshot(self, snapshot_date: str) -> bool:
        cur = await self._conn.execute(
            "SELECT 1 FROM leaderboard_snapshots WHERE snapshot_date=?",
            (snapshot_date,),
        )
        return await cur.fetchone() is not None

    async def load_leaderboard_snapshot(self, snapshot_date: str):
        cur = await self._conn.execute(
            "SELECT payload FROM leaderboard_snapshots WHERE snapshot_date=?",
            (snapshot_date,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return json.loads(zlib.decompress(row["payload"]).decode("utf-8"))

    async def list_leaderboard_snapshot_dates(self) -> list[str]:
        cur = await self._conn.execute(
            "SELECT snapshot_date FROM leaderboard_snapshots ORDER BY snapshot_date",
        )
        return [r["snapshot_date"] for r in await cur.fetchall()]

    async def upsert_leaderboard_accounts(
        self, snapshot_date: str, accounts: dict[str, float],
    ) -> None:
        for address, av in accounts.items():
            await self._conn.execute(
                "INSERT OR REPLACE INTO leaderboard_accounts "
                "(address, snapshot_date, account_value) VALUES (?,?,?)",
                (address.lower(), snapshot_date, float(av)),
            )
        await self._conn.commit()

    async def account_values(self, address: str) -> list[tuple[str, float]]:
        cur = await self._conn.execute(
            "SELECT snapshot_date, account_value FROM leaderboard_accounts "
            "WHERE address=? ORDER BY snapshot_date",
            (address.lower(),),
        )
        return [
            (r["snapshot_date"], float(r["account_value"]))
            for r in await cur.fetchall()
        ]

    async def save_wallet_state(
        self, address: str, snapshot_date: str, *, account_value: float, raw_body,
    ) -> None:
        payload = zlib.compress(json.dumps(raw_body).encode("utf-8"), level=6)
        await self._conn.execute(
            "INSERT OR REPLACE INTO wallet_state_snapshots "
            "(address, snapshot_date, account_value, payload) VALUES (?,?,?,?)",
            (address.lower(), snapshot_date, float(account_value), payload),
        )
        await self._conn.commit()

    # ---- backtest run log (every trial persisted; nothing auto-selected) ----

    async def save_backtest_run(
        self, run_id: str, *, spec: dict, params: dict, summary: dict,
    ) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO backtest_runs "
            "(run_id, started_at, spec_json, params_json, summary_json) "
            "VALUES (?,?,?,?,?)",
            (
                run_id, int(time.time()), json.dumps(spec),
                json.dumps(params), json.dumps(summary),
            ),
        )
        await self._conn.commit()

    async def save_backtest_trades(
        self, run_id: str, params_id: str, trades: list[dict],
    ) -> None:
        for t in trades:
            await self._conn.execute(
                "INSERT INTO backtest_trades (run_id, params_id, trade_json) "
                "VALUES (?,?,?)",
                (run_id, params_id, json.dumps(t)),
            )
        await self._conn.commit()

    # ---- retention ---------------------------------------------------------------

    async def prune(
        self, *, now_ms: int | None = None,
        candle_1m_keep_days: int = 90,
        candle_15m_keep_days: int = 400,
        fills_keep_days: int = 180,
        keep_runs: int = 20,
    ) -> None:
        now_ms = now_ms or int(time.time() * 1000)
        day = 86_400_000
        await self._conn.execute(
            "DELETE FROM candles WHERE interval='1m' AND ts < ?",
            (now_ms - candle_1m_keep_days * day,),
        )
        await self._conn.execute(
            "DELETE FROM candles WHERE interval='15m' AND ts < ?",
            (now_ms - candle_15m_keep_days * day,),
        )
        await self._conn.execute(
            "DELETE FROM fills_cache WHERE ts < ?",
            (now_ms - fills_keep_days * day,),
        )
        await self._conn.execute(
            "DELETE FROM backtest_trades WHERE run_id NOT IN "
            "(SELECT run_id FROM backtest_runs ORDER BY started_at DESC LIMIT ?)",
            (keep_runs,),
        )
        await self._conn.commit()
        # reclaim in small steps; a full VACUUM would block the bot
        await self._conn.execute("PRAGMA incremental_vacuum")
        await self._conn.commit()
