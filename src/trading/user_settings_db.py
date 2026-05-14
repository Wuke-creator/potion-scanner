"""Per-user trading preferences: slippage tolerance + size-preset list.

Schema:

  user_trading_settings (
    telegram_user_id  INTEGER PRIMARY KEY
    slippage_bps      INTEGER NOT NULL DEFAULT 50    -- 0.5% by default
    size_presets_json TEXT NOT NULL DEFAULT '[25,50,100,250]'
    last_used_size    REAL                            -- nullable
    updated_at        INTEGER NOT NULL
  )

Stored as JSON so we can flex the preset count without a migration.
Default presets cover beginner ($25) through whale-lite ($250); user
edits via /settings.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


DEFAULT_SLIPPAGE_BPS = 50           # 0.5%
DEFAULT_SIZE_PRESETS = [25, 50, 100, 250]
MAX_PRESETS = 6
MIN_SLIPPAGE_BPS = 1                # 0.01%
MAX_SLIPPAGE_BPS = 1000             # 10%


@dataclass
class UserTradingSettings:
    telegram_user_id: int
    slippage_bps: int
    size_presets: list[float]
    last_used_size: float | None
    updated_at: int
    last_used_leverage: int | None = None


_DDL = """
CREATE TABLE IF NOT EXISTS user_trading_settings (
  telegram_user_id  INTEGER PRIMARY KEY,
  slippage_bps      INTEGER NOT NULL DEFAULT 50,
  size_presets_json TEXT NOT NULL DEFAULT '[25,50,100,250]',
  last_used_size    REAL,
  updated_at        INTEGER NOT NULL,
  last_used_leverage INTEGER
);
"""

# Migration for DBs that predate the leverage column. SQLite rejects ALTER
# if the column already exists, so we swallow.
_MIGRATIONS = (
    "ALTER TABLE user_trading_settings ADD COLUMN last_used_leverage INTEGER",
)


class UserTradingSettingsDB:
    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(_DDL)
        for stmt in _MIGRATIONS:
            try:
                await self._conn.execute(stmt)
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    logger.debug("user_trading_settings migration skipped: %s", e)
        await self._conn.commit()
        logger.info("User trading settings DB opened at %s", self._db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def get_or_default(self, telegram_user_id: int) -> UserTradingSettings:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT telegram_user_id, slippage_bps, size_presets_json, "
            "       last_used_size, updated_at, last_used_leverage "
            "FROM user_trading_settings WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return UserTradingSettings(
                telegram_user_id=telegram_user_id,
                slippage_bps=DEFAULT_SLIPPAGE_BPS,
                size_presets=list(DEFAULT_SIZE_PRESETS),
                last_used_size=None,
                updated_at=0,
                last_used_leverage=None,
            )
        try:
            presets = json.loads(row[2])
            if not isinstance(presets, list):
                presets = list(DEFAULT_SIZE_PRESETS)
        except (json.JSONDecodeError, TypeError):
            presets = list(DEFAULT_SIZE_PRESETS)
        return UserTradingSettings(
            telegram_user_id=row[0],
            slippage_bps=int(row[1]),
            size_presets=[float(p) for p in presets],
            last_used_size=float(row[3]) if row[3] is not None else None,
            updated_at=int(row[4]),
            last_used_leverage=int(row[5]) if row[5] is not None else None,
        )

    async def set_last_used_leverage(
        self, telegram_user_id: int, leverage: int,
    ) -> None:
        if leverage < 1 or leverage > 500:
            raise ValueError(f"leverage {leverage} outside [1, 500]")
        assert self._conn is not None
        now = int(time.time())
        await self._conn.execute(
            "INSERT INTO user_trading_settings "
            "(telegram_user_id, last_used_leverage, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET "
            "  last_used_leverage = excluded.last_used_leverage, "
            "  updated_at = excluded.updated_at",
            (telegram_user_id, int(leverage), now),
        )
        await self._conn.commit()

    async def set_slippage(
        self, telegram_user_id: int, slippage_bps: int,
    ) -> None:
        if slippage_bps < MIN_SLIPPAGE_BPS or slippage_bps > MAX_SLIPPAGE_BPS:
            raise ValueError(
                f"slippage_bps {slippage_bps} outside "
                f"[{MIN_SLIPPAGE_BPS}, {MAX_SLIPPAGE_BPS}]"
            )
        assert self._conn is not None
        now = int(time.time())
        await self._conn.execute(
            "INSERT INTO user_trading_settings "
            "(telegram_user_id, slippage_bps, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET "
            "  slippage_bps = excluded.slippage_bps, "
            "  updated_at = excluded.updated_at",
            (telegram_user_id, slippage_bps, now),
        )
        await self._conn.commit()

    async def set_presets(
        self, telegram_user_id: int, presets: list[float],
    ) -> None:
        if not presets:
            raise ValueError("presets must not be empty")
        if len(presets) > MAX_PRESETS:
            raise ValueError(
                f"presets must be at most {MAX_PRESETS} entries"
            )
        for p in presets:
            if p <= 0:
                raise ValueError("preset sizes must be positive")
        deduped = sorted({round(float(p), 2) for p in presets})
        assert self._conn is not None
        now = int(time.time())
        await self._conn.execute(
            "INSERT INTO user_trading_settings "
            "(telegram_user_id, size_presets_json, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET "
            "  size_presets_json = excluded.size_presets_json, "
            "  updated_at = excluded.updated_at",
            (telegram_user_id, json.dumps(deduped), now),
        )
        await self._conn.commit()

    async def mark_size_used(
        self, telegram_user_id: int, size: float,
    ) -> None:
        assert self._conn is not None
        now = int(time.time())
        await self._conn.execute(
            "INSERT INTO user_trading_settings "
            "(telegram_user_id, last_used_size, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET "
            "  last_used_size = excluded.last_used_size, "
            "  updated_at = excluded.updated_at",
            (telegram_user_id, float(size), now),
        )
        await self._conn.commit()
