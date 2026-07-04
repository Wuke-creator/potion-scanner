"""Per-user autotrade preferences + fire ledger.

Two tables:

  autotrade_prefs      opt-in flag, size percent, disclosure acceptance,
                       and a per-day trade counter for the daily cap.

  autotrade_fires      one row per (user, signal) actually acted on, used
                       as an idempotency guard so a re-broadcast of the
                       same signal can never double-fire.

A user only auto-trades when enabled=1 AND disclosure_accepted_at is set
(both gated behind the /autotrade command). The allowlist and delegate
checks live in the engine, not here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

DEFAULT_SIZE_PCT = 5.0
MAX_SIZE_PCT = 100.0
MIN_SIZE_PCT = 0.1


@dataclass
class AutotradePrefs:
    telegram_user_id: int
    enabled: bool
    size_pct: float
    disclosure_accepted_at: int | None
    trades_today: int
    trades_today_date: str
    updated_at: int

    @property
    def ready(self) -> bool:
        """True when the user has opted in and accepted the disclosure."""
        return self.enabled and self.disclosure_accepted_at is not None


_DDL_PREFS = """
CREATE TABLE IF NOT EXISTS autotrade_prefs (
  telegram_user_id        INTEGER PRIMARY KEY,
  enabled                 INTEGER NOT NULL DEFAULT 0,
  size_pct                REAL NOT NULL DEFAULT 5.0,
  disclosure_accepted_at  INTEGER,
  trades_today            INTEGER NOT NULL DEFAULT 0,
  trades_today_date       TEXT NOT NULL DEFAULT '',
  updated_at              INTEGER NOT NULL DEFAULT 0
);
"""

_DDL_FIRES = """
CREATE TABLE IF NOT EXISTS autotrade_fires (
  telegram_user_id  INTEGER NOT NULL,
  signal_id         INTEGER NOT NULL,
  fired_at          INTEGER NOT NULL,
  PRIMARY KEY (telegram_user_id, signal_id)
);
"""

# Peak account value per user, for the risk guard's drawdown brake
# (src/trading/autotrade_risk.py). Withdrawals do not lower the peak;
# resetting after a withdrawal = DELETE the row (documented operator action).
_DDL_PEAKS = """
CREATE TABLE IF NOT EXISTS autotrade_equity_peaks (
  telegram_user_id  INTEGER PRIMARY KEY,
  peak_value        REAL NOT NULL,
  updated_at        INTEGER NOT NULL
);
"""


def _utc_date(now: int | None = None) -> str:
    ts = time.time() if now is None else now
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


class AutotradePrefsDB:
    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(_DDL_PREFS)
        await self._conn.execute(_DDL_FIRES)
        await self._conn.execute(_DDL_PEAKS)
        await self._conn.commit()
        logger.info("Autotrade prefs DB opened at %s", self._db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def get_or_default(
        self, telegram_user_id: int, *, default_pct: float = DEFAULT_SIZE_PCT,
    ) -> AutotradePrefs:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT telegram_user_id, enabled, size_pct, "
            "       disclosure_accepted_at, trades_today, "
            "       trades_today_date, updated_at "
            "FROM autotrade_prefs WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return AutotradePrefs(
                telegram_user_id=telegram_user_id,
                enabled=False,
                size_pct=default_pct,
                disclosure_accepted_at=None,
                trades_today=0,
                trades_today_date="",
                updated_at=0,
            )
        return AutotradePrefs(
            telegram_user_id=row[0],
            enabled=bool(row[1]),
            size_pct=float(row[2]),
            disclosure_accepted_at=row[3],
            trades_today=int(row[4]),
            trades_today_date=row[5],
            updated_at=int(row[6]),
        )

    async def _upsert_field(self, telegram_user_id: int, column: str, value) -> None:
        assert self._conn is not None
        now = int(time.time())
        await self._conn.execute(
            f"INSERT INTO autotrade_prefs (telegram_user_id, {column}, updated_at) "
            f"VALUES (?, ?, ?) "
            f"ON CONFLICT(telegram_user_id) DO UPDATE SET "
            f"  {column} = excluded.{column}, updated_at = excluded.updated_at",
            (telegram_user_id, value, now),
        )
        await self._conn.commit()

    async def set_enabled(self, telegram_user_id: int, enabled: bool) -> None:
        await self._upsert_field(telegram_user_id, "enabled", 1 if enabled else 0)

    async def set_size_pct(self, telegram_user_id: int, pct: float) -> None:
        if pct < MIN_SIZE_PCT or pct > MAX_SIZE_PCT:
            raise ValueError(
                f"size_pct {pct} outside [{MIN_SIZE_PCT}, {MAX_SIZE_PCT}]"
            )
        await self._upsert_field(telegram_user_id, "size_pct", float(pct))

    async def accept_disclosure(self, telegram_user_id: int) -> None:
        await self._upsert_field(
            telegram_user_id, "disclosure_accepted_at", int(time.time()),
        )

    async def try_claim_fire(
        self, telegram_user_id: int, signal_id: int,
    ) -> bool:
        """Atomically claim (user, signal). Returns True if newly claimed.

        False means we already fired for this signal -> skip. This is the
        dedupe guard against re-broadcasts of the same signal.
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            "INSERT OR IGNORE INTO autotrade_fires "
            "(telegram_user_id, signal_id, fired_at) VALUES (?, ?, ?)",
            (telegram_user_id, signal_id, int(time.time())),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def release_fire(self, telegram_user_id: int, signal_id: int) -> None:
        """Undo a claim (e.g. sizing skipped the trade) so it isn't counted."""
        assert self._conn is not None
        await self._conn.execute(
            "DELETE FROM autotrade_fires "
            "WHERE telegram_user_id = ? AND signal_id = ?",
            (telegram_user_id, signal_id),
        )
        await self._conn.commit()

    async def try_consume_daily_slot(
        self, telegram_user_id: int, max_per_day: int, *, now: int | None = None,
    ) -> bool:
        """Increment today's counter if under the cap. Returns False when capped.

        Resets the counter when the UTC date rolls over.
        """
        assert self._conn is not None
        today = _utc_date(now)
        prefs = await self.get_or_default(telegram_user_id)
        current = prefs.trades_today if prefs.trades_today_date == today else 0
        if current >= max_per_day:
            return False
        ts = int(time.time()) if now is None else now
        await self._conn.execute(
            "INSERT INTO autotrade_prefs "
            "(telegram_user_id, trades_today, trades_today_date, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET "
            "  trades_today = ?, trades_today_date = ?, updated_at = ?",
            (
                telegram_user_id, current + 1, today, ts,
                current + 1, today, ts,
            ),
        )
        await self._conn.commit()
        return True

    async def bump_equity_peak(
        self, telegram_user_id: int, account_value: float, now: int | None = None,
    ) -> float:
        """Record ``account_value`` as the new peak if it is one; return the
        current peak either way. Used by the risk guard's drawdown brake."""
        assert self._conn is not None
        ts = int(time.time()) if now is None else now
        await self._conn.execute(
            "INSERT INTO autotrade_equity_peaks (telegram_user_id, peak_value, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET "
            "  peak_value = MAX(peak_value, excluded.peak_value), "
            "  updated_at = excluded.updated_at",
            (telegram_user_id, float(account_value), ts),
        )
        await self._conn.commit()
        async with self._conn.execute(
            "SELECT peak_value FROM autotrade_equity_peaks WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return float(row[0]) if row else float(account_value)
