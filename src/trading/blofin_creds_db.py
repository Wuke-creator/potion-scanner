"""Encrypted per-user Blofin API credential store.

Each selected user creates a Blofin API key (Trade + Read, no withdrawal
permission, ideally IP-locked to the server) and connects it once via
/connect. The three secrets (key, secret, passphrase) are Fernet-encrypted
at rest and only decrypted immediately before a signed request.

Schema:

  blofin_creds (
    telegram_user_id       INTEGER PRIMARY KEY
    api_key_encrypted      TEXT NOT NULL
    api_secret_encrypted   TEXT NOT NULL
    passphrase_encrypted   TEXT NOT NULL
    connected_at           INTEGER NOT NULL
    last_trade_at          INTEGER
    last_failure_at        INTEGER
    last_failure_reason    TEXT
    is_active              INTEGER NOT NULL DEFAULT 1
  )

Never log the decrypted values.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from src.crypto import decrypt, encrypt
from src.trading.blofin_client import BlofinCreds

logger = logging.getLogger(__name__)


@dataclass
class BlofinCredRecord:
    telegram_user_id: int
    connected_at: int
    last_trade_at: int | None
    last_failure_at: int | None
    last_failure_reason: str | None
    is_active: bool


_DDL = """
CREATE TABLE IF NOT EXISTS blofin_creds (
  telegram_user_id       INTEGER PRIMARY KEY,
  api_key_encrypted      TEXT NOT NULL,
  api_secret_encrypted   TEXT NOT NULL,
  passphrase_encrypted   TEXT NOT NULL,
  connected_at           INTEGER NOT NULL,
  last_trade_at          INTEGER,
  last_failure_at        INTEGER,
  last_failure_reason    TEXT,
  is_active              INTEGER NOT NULL DEFAULT 1
);
"""

_INDEX = """
CREATE INDEX IF NOT EXISTS idx_blofin_creds_active
    ON blofin_creds (is_active);
"""


class BlofinCredsDB:
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
        logger.info("Blofin creds DB opened at %s", self._db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def upsert(
        self,
        telegram_user_id: int,
        api_key: str,
        api_secret: str,
        passphrase: str,
    ) -> None:
        assert self._conn is not None
        now = int(time.time())
        await self._conn.execute(
            "INSERT INTO blofin_creds "
            "(telegram_user_id, api_key_encrypted, api_secret_encrypted, "
            " passphrase_encrypted, connected_at, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET "
            "  api_key_encrypted = excluded.api_key_encrypted, "
            "  api_secret_encrypted = excluded.api_secret_encrypted, "
            "  passphrase_encrypted = excluded.passphrase_encrypted, "
            "  connected_at = excluded.connected_at, "
            "  last_failure_at = NULL, "
            "  last_failure_reason = NULL, "
            "  is_active = 1",
            (
                telegram_user_id,
                encrypt(api_key),
                encrypt(api_secret),
                encrypt(passphrase),
                now,
            ),
        )
        await self._conn.commit()

    async def get(self, telegram_user_id: int) -> BlofinCredRecord | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT telegram_user_id, connected_at, last_trade_at, "
            "       last_failure_at, last_failure_reason, is_active "
            "FROM blofin_creds WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return BlofinCredRecord(
            telegram_user_id=row[0],
            connected_at=row[1],
            last_trade_at=row[2],
            last_failure_at=row[3],
            last_failure_reason=row[4],
            is_active=bool(row[5]),
        )

    async def get_creds(self, telegram_user_id: int) -> BlofinCreds | None:
        """Return decrypted creds, or None if not connected / inactive.

        Used only on the trade path. Never log the return value.
        """
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT api_key_encrypted, api_secret_encrypted, "
            "       passphrase_encrypted, is_active "
            "FROM blofin_creds WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or not bool(row[3]):
            return None
        return BlofinCreds(
            api_key=decrypt(row[0]),
            api_secret=decrypt(row[1]),
            passphrase=decrypt(row[2]),
        )

    async def deactivate(self, telegram_user_id: int) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE blofin_creds SET is_active = 0 WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        await self._conn.commit()

    async def delete(self, telegram_user_id: int) -> None:
        """Permanently remove the creds. Triggered by /disconnect."""
        assert self._conn is not None
        await self._conn.execute(
            "DELETE FROM blofin_creds WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        await self._conn.commit()

    async def mark_trade_success(self, telegram_user_id: int) -> None:
        assert self._conn is not None
        now = int(time.time())
        await self._conn.execute(
            "UPDATE blofin_creds SET last_trade_at = ?, "
            "  last_failure_at = NULL, last_failure_reason = NULL "
            "WHERE telegram_user_id = ?",
            (now, telegram_user_id),
        )
        await self._conn.commit()

    async def mark_trade_failure(
        self, telegram_user_id: int, reason: str,
    ) -> None:
        assert self._conn is not None
        now = int(time.time())
        await self._conn.execute(
            "UPDATE blofin_creds SET last_failure_at = ?, "
            "  last_failure_reason = ? WHERE telegram_user_id = ?",
            (now, reason[:500], telegram_user_id),
        )
        await self._conn.commit()

    async def count_active(self) -> int:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COUNT(*) FROM blofin_creds WHERE is_active = 1"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0
