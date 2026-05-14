"""SQLite store for per-user Ostium delegate keys.

Schema:

  trading_delegates (
    telegram_user_id              INTEGER PRIMARY KEY
    trader_address                TEXT NOT NULL  -- 0x... EOA holding USDC
    delegate_private_key_encrypted TEXT NOT NULL -- Fernet ciphertext of the
                                                    delegate EOA's private key
    connected_at                  INTEGER NOT NULL
    last_trade_at                 INTEGER          -- nullable; bumped on submit
    last_failure_at               INTEGER          -- nullable; bumped on error
    last_failure_reason           TEXT             -- nullable
    is_active                     INTEGER NOT NULL DEFAULT 1
  )

The private key never leaves this process in plaintext except over the
inter-service call to the trade-executor sidecar on Railway's private
network, where it is decrypted immediately before submitting the
transaction and dropped from memory afterwards.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from src.crypto import decrypt, encrypt

logger = logging.getLogger(__name__)


@dataclass
class TradingDelegate:
    telegram_user_id: int
    trader_address: str
    delegate_private_key_encrypted: str
    connected_at: int
    last_trade_at: int | None
    last_failure_at: int | None
    last_failure_reason: str | None
    is_active: bool


_DDL = """
CREATE TABLE IF NOT EXISTS trading_delegates (
  telegram_user_id              INTEGER PRIMARY KEY,
  trader_address                TEXT NOT NULL,
  delegate_private_key_encrypted TEXT NOT NULL,
  connected_at                  INTEGER NOT NULL,
  last_trade_at                 INTEGER,
  last_failure_at               INTEGER,
  last_failure_reason           TEXT,
  is_active                     INTEGER NOT NULL DEFAULT 1
);
"""

_INDEX = """
CREATE INDEX IF NOT EXISTS idx_trading_delegates_active
    ON trading_delegates (is_active);
"""


class DelegatesDB:
    """aiosqlite-backed delegate key store."""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(_DDL)
        await self._conn.execute(_INDEX)
        await self._conn.commit()
        logger.info("Trading delegates DB opened at %s", self._db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def upsert(
        self,
        telegram_user_id: int,
        trader_address: str,
        delegate_private_key: str,
    ) -> None:
        assert self._conn is not None
        ciphertext = encrypt(delegate_private_key)
        now = int(time.time())
        await self._conn.execute(
            "INSERT INTO trading_delegates "
            "(telegram_user_id, trader_address, "
            " delegate_private_key_encrypted, connected_at, is_active) "
            "VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET "
            "  trader_address = excluded.trader_address, "
            "  delegate_private_key_encrypted = excluded.delegate_private_key_encrypted, "
            "  connected_at = excluded.connected_at, "
            "  last_failure_at = NULL, "
            "  last_failure_reason = NULL, "
            "  is_active = 1",
            (telegram_user_id, trader_address, ciphertext, now),
        )
        await self._conn.commit()

    async def get(self, telegram_user_id: int) -> TradingDelegate | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT telegram_user_id, trader_address, "
            "       delegate_private_key_encrypted, connected_at, "
            "       last_trade_at, last_failure_at, "
            "       last_failure_reason, is_active "
            "FROM trading_delegates WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return TradingDelegate(
            telegram_user_id=row[0],
            trader_address=row[1],
            delegate_private_key_encrypted=row[2],
            connected_at=row[3],
            last_trade_at=row[4],
            last_failure_at=row[5],
            last_failure_reason=row[6],
            is_active=bool(row[7]),
        )

    async def get_plaintext_key(self, telegram_user_id: int) -> str | None:
        """Return the decrypted delegate private key, or None if not connected.

        Used only by the trade submission path. Never log the return value.
        """
        record = await self.get(telegram_user_id)
        if record is None or not record.is_active:
            return None
        return decrypt(record.delegate_private_key_encrypted)

    async def deactivate(self, telegram_user_id: int) -> None:
        """Mark as inactive without deleting (audit trail). Use delete() to wipe."""
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE trading_delegates SET is_active = 0 WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        await self._conn.commit()

    async def delete(self, telegram_user_id: int) -> None:
        """Permanently remove the delegate. Triggered by /disconnect."""
        assert self._conn is not None
        await self._conn.execute(
            "DELETE FROM trading_delegates WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        await self._conn.commit()

    async def mark_trade_success(self, telegram_user_id: int) -> None:
        assert self._conn is not None
        now = int(time.time())
        await self._conn.execute(
            "UPDATE trading_delegates "
            "SET last_trade_at = ?, "
            "    last_failure_at = NULL, "
            "    last_failure_reason = NULL "
            "WHERE telegram_user_id = ?",
            (now, telegram_user_id),
        )
        await self._conn.commit()

    async def mark_trade_failure(
        self, telegram_user_id: int, reason: str,
    ) -> None:
        assert self._conn is not None
        now = int(time.time())
        truncated = reason[:500]
        await self._conn.execute(
            "UPDATE trading_delegates "
            "SET last_failure_at = ?, last_failure_reason = ? "
            "WHERE telegram_user_id = ?",
            (now, truncated, telegram_user_id),
        )
        await self._conn.commit()

    async def count_active(self) -> int:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COUNT(*) FROM trading_delegates WHERE is_active = 1"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0
