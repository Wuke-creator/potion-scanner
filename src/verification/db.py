"""SQLite store for verified users + pending OAuth verifications.

Two tables:

  - verified_users: one row per Telegram user who has authorized via
    Discord and currently holds the Elite role in the Potion guild.
    Stores the encrypted Discord refresh token so the reverify cron can
    re-check the role without requiring the user to log in again.

  - pending_verifications: short-lived rows mapping a state token (which
    we hand the user as part of the OAuth URL) to its PKCE verifier and
    Telegram user ID. The OAuth callback consumes the row to finish the
    flow.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass
class VerifiedUser:
    telegram_user_id: int
    discord_user_id: str
    refresh_token_encrypted: str
    verified_at: int          # epoch seconds
    last_checked_at: int      # epoch seconds
    is_active: bool
    email: str = ""           # captured via Discord OAuth email scope; blank until re-verify
    last_reminder_sent_at: int = 0  # for Feature 3 value reminder cron


@dataclass
class PendingVerification:
    state: str
    telegram_user_id: int
    code_verifier: str
    created_at: int


_VERIFIED_DDL = """
CREATE TABLE IF NOT EXISTS verified_users (
  telegram_user_id        INTEGER PRIMARY KEY,
  discord_user_id         TEXT NOT NULL,
  refresh_token_encrypted TEXT NOT NULL,
  verified_at             INTEGER NOT NULL,
  last_checked_at         INTEGER NOT NULL,
  is_active               INTEGER NOT NULL DEFAULT 1,
  email                   TEXT NOT NULL DEFAULT '',
  last_reminder_sent_at   INTEGER NOT NULL DEFAULT 0
);
"""

# Migrations for existing DBs that predate the email + last_reminder columns.
# SQLite rejects ALTER if column already exists, so we swallow the error.
_VERIFIED_MIGRATIONS = (
    "ALTER TABLE verified_users ADD COLUMN email TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE verified_users ADD COLUMN last_reminder_sent_at INTEGER NOT NULL DEFAULT 0",
)

_VERIFIED_INDEX = """
CREATE INDEX IF NOT EXISTS idx_verified_active_lastchecked
    ON verified_users (is_active, last_checked_at);
"""

_PENDING_DDL = """
CREATE TABLE IF NOT EXISTS pending_verifications (
  state             TEXT PRIMARY KEY,
  telegram_user_id  INTEGER NOT NULL,
  code_verifier     TEXT NOT NULL,
  created_at        INTEGER NOT NULL
);
"""

_PENDING_INDEX = """
CREATE INDEX IF NOT EXISTS idx_pending_created
    ON pending_verifications (created_at);
"""

_SUBSCRIPTIONS_DDL = """
CREATE TABLE IF NOT EXISTS user_subscriptions (
  telegram_user_id  INTEGER NOT NULL,
  channel_key       TEXT NOT NULL,
  PRIMARY KEY (telegram_user_id, channel_key)
);
"""

_MUTED_TOKENS_DDL = """
CREATE TABLE IF NOT EXISTS muted_tokens (
  telegram_user_id  INTEGER NOT NULL,
  token             TEXT NOT NULL,
  PRIMARY KEY (telegram_user_id, token)
);
"""


class VerificationDB:
    """Async SQLite wrapper. One connection, used from a single event loop."""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(_VERIFIED_DDL)
        await self._conn.execute(_VERIFIED_INDEX)
        # Apply column migrations for existing DBs that predate email + reminder columns
        for stmt in _VERIFIED_MIGRATIONS:
            try:
                await self._conn.execute(stmt)
            except Exception as e:
                # Column already exists or similar -> fine, move on
                if "duplicate column" not in str(e).lower():
                    logger.debug("Migration skipped: %s", e)
        await self._conn.execute(_PENDING_DDL)
        await self._conn.execute(_PENDING_INDEX)
        await self._conn.execute(_SUBSCRIPTIONS_DDL)
        await self._conn.execute(_MUTED_TOKENS_DDL)
        await self._conn.commit()
        logger.info("Verification DB opened at %s", self._db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---- pending_verifications ----------------------------------------

    async def store_pending(
        self, state: str, telegram_user_id: int, code_verifier: str,
    ) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "INSERT OR REPLACE INTO pending_verifications "
            "(state, telegram_user_id, code_verifier, created_at) "
            "VALUES (?, ?, ?, ?)",
            (state, telegram_user_id, code_verifier, int(time.time())),
        )
        await self._conn.commit()

    async def consume_pending(self, state: str) -> PendingVerification | None:
        """Atomically read + delete a pending verification by state token."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT state, telegram_user_id, code_verifier, created_at "
            "FROM pending_verifications WHERE state = ?",
            (state,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        await self._conn.execute(
            "DELETE FROM pending_verifications WHERE state = ?", (state,),
        )
        await self._conn.commit()
        return PendingVerification(
            state=row[0],
            telegram_user_id=row[1],
            code_verifier=row[2],
            created_at=row[3],
        )

    async def cleanup_expired_pending(self, max_age_seconds: int) -> int:
        """Delete pending rows older than max_age_seconds. Returns count deleted."""
        assert self._conn is not None
        cutoff = int(time.time()) - max_age_seconds
        async with self._conn.execute(
            "DELETE FROM pending_verifications WHERE created_at < ?", (cutoff,),
        ) as cursor:
            await self._conn.commit()
            return cursor.rowcount or 0

    # ---- verified_users -----------------------------------------------

    async def upsert_verified(
        self,
        telegram_user_id: int,
        discord_user_id: str,
        refresh_token_encrypted: str,
        email: str = "",
    ) -> None:
        assert self._conn is not None
        now = int(time.time())
        # Only overwrite email if a non-empty value is provided, so a
        # re-verification without fresh email doesn't blank out a stored one.
        if email:
            await self._conn.execute(
                "INSERT INTO verified_users "
                "(telegram_user_id, discord_user_id, "
                " refresh_token_encrypted, verified_at, last_checked_at, "
                " is_active, email) "
                "VALUES (?, ?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(telegram_user_id) DO UPDATE SET "
                "  discord_user_id = excluded.discord_user_id, "
                "  refresh_token_encrypted = excluded.refresh_token_encrypted, "
                "  last_checked_at = excluded.last_checked_at, "
                "  email = excluded.email, "
                "  is_active = 1",
                (
                    telegram_user_id, discord_user_id,
                    refresh_token_encrypted, now, now, email,
                ),
            )
        else:
            await self._conn.execute(
                "INSERT INTO verified_users "
                "(telegram_user_id, discord_user_id, "
                " refresh_token_encrypted, verified_at, last_checked_at, is_active) "
                "VALUES (?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(telegram_user_id) DO UPDATE SET "
                "  discord_user_id = excluded.discord_user_id, "
                "  refresh_token_encrypted = excluded.refresh_token_encrypted, "
                "  last_checked_at = excluded.last_checked_at, "
                "  is_active = 1",
                (
                    telegram_user_id, discord_user_id,
                    refresh_token_encrypted, now, now,
                ),
            )
        await self._conn.commit()

    async def get_verified(self, telegram_user_id: int) -> VerifiedUser | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT telegram_user_id, discord_user_id, "
            "       refresh_token_encrypted, verified_at, last_checked_at, "
            "       is_active, email, last_reminder_sent_at "
            "FROM verified_users WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return VerifiedUser(
            telegram_user_id=row[0],
            discord_user_id=row[1],
            refresh_token_encrypted=row[2],
            verified_at=row[3],
            last_checked_at=row[4],
            is_active=bool(row[5]),
            email=row[6] or "",
            last_reminder_sent_at=int(row[7] or 0),
        )

    async def list_active(self) -> list[VerifiedUser]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT telegram_user_id, discord_user_id, "
            "       refresh_token_encrypted, verified_at, last_checked_at, "
            "       is_active, email, last_reminder_sent_at "
            "FROM verified_users WHERE is_active = 1 "
            "ORDER BY last_checked_at ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            VerifiedUser(
                telegram_user_id=r[0],
                discord_user_id=r[1],
                refresh_token_encrypted=r[2],
                verified_at=r[3],
                last_checked_at=r[4],
                is_active=bool(r[5]),
                email=r[6] or "",
                last_reminder_sent_at=int(r[7] or 0),
            )
            for r in rows
        ]

    async def list_active_with_email(self) -> list[VerifiedUser]:
        """Active users who have a non-empty email (for email-based automations)."""
        rows = await self.list_active()
        return [r for r in rows if r.email]

    async def update_email(self, telegram_user_id: int, email: str) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE verified_users SET email = ? WHERE telegram_user_id = ?",
            (email, telegram_user_id),
        )
        await self._conn.commit()

    async def mark_reminder_sent(
        self, telegram_user_id: int, when: int | None = None,
    ) -> None:
        assert self._conn is not None
        ts = when if when is not None else int(time.time())
        await self._conn.execute(
            "UPDATE verified_users SET last_reminder_sent_at = ? "
            "WHERE telegram_user_id = ?",
            (ts, telegram_user_id),
        )
        await self._conn.commit()

    async def list_active_user_ids(self) -> list[int]:
        """Return all active verified Telegram user IDs (no subscription filter)."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT telegram_user_id FROM verified_users WHERE is_active = 1"
        ) as cursor:
            rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def list_subscribed_user_ids(self, channel_key: str) -> list[int]:
        """Hot path for the dispatcher: active users subscribed to a channel.

        Single indexed query, returns only Telegram user IDs.
        """
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT s.telegram_user_id "
            "FROM user_subscriptions s "
            "JOIN verified_users v ON s.telegram_user_id = v.telegram_user_id "
            "WHERE s.channel_key = ? AND v.is_active = 1",
            (channel_key,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def count_active(self) -> int:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COUNT(*) FROM verified_users WHERE is_active = 1"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def update_after_recheck(
        self,
        telegram_user_id: int,
        is_active: bool,
        new_refresh_token_encrypted: str | None = None,
    ) -> None:
        assert self._conn is not None
        now = int(time.time())
        if new_refresh_token_encrypted is not None:
            await self._conn.execute(
                "UPDATE verified_users SET "
                "  is_active = ?, "
                "  last_checked_at = ?, "
                "  refresh_token_encrypted = ? "
                "WHERE telegram_user_id = ?",
                (1 if is_active else 0, now, new_refresh_token_encrypted, telegram_user_id),
            )
        else:
            await self._conn.execute(
                "UPDATE verified_users SET "
                "  is_active = ?, "
                "  last_checked_at = ? "
                "WHERE telegram_user_id = ?",
                (1 if is_active else 0, now, telegram_user_id),
            )
        await self._conn.commit()

    # ---- user_subscriptions -------------------------------------------

    async def get_subscriptions(self, telegram_user_id: int) -> set[str]:
        """Return the set of channel_keys the user is subscribed to."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT channel_key FROM user_subscriptions WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return {r[0] for r in rows}

    async def toggle_subscription(
        self, telegram_user_id: int, channel_key: str,
    ) -> bool:
        """Toggle a subscription. Returns True if now subscribed, False if unsubscribed."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT 1 FROM user_subscriptions "
            "WHERE telegram_user_id = ? AND channel_key = ?",
            (telegram_user_id, channel_key),
        ) as cursor:
            exists = await cursor.fetchone() is not None

        if exists:
            await self._conn.execute(
                "DELETE FROM user_subscriptions "
                "WHERE telegram_user_id = ? AND channel_key = ?",
                (telegram_user_id, channel_key),
            )
            await self._conn.commit()
            return False
        else:
            await self._conn.execute(
                "INSERT INTO user_subscriptions (telegram_user_id, channel_key) "
                "VALUES (?, ?)",
                (telegram_user_id, channel_key),
            )
            await self._conn.commit()
            return True

    # ---- muted_tokens -------------------------------------------------

    async def get_muted_tokens(self, telegram_user_id: int) -> set[str]:
        """Return the set of token symbols the user has muted (e.g. {'ETH', 'DOGE'})."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT token FROM muted_tokens WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return {r[0] for r in rows}

    async def toggle_muted_token(
        self, telegram_user_id: int, token: str,
    ) -> bool:
        """Toggle a token mute. Returns True if now muted, False if unmuted."""
        assert self._conn is not None
        token = token.upper().strip()
        async with self._conn.execute(
            "SELECT 1 FROM muted_tokens "
            "WHERE telegram_user_id = ? AND token = ?",
            (telegram_user_id, token),
        ) as cursor:
            exists = await cursor.fetchone() is not None

        if exists:
            await self._conn.execute(
                "DELETE FROM muted_tokens WHERE telegram_user_id = ? AND token = ?",
                (telegram_user_id, token),
            )
            await self._conn.commit()
            return False
        else:
            await self._conn.execute(
                "INSERT INTO muted_tokens (telegram_user_id, token) VALUES (?, ?)",
                (telegram_user_id, token),
            )
            await self._conn.commit()
            return True

    async def is_token_muted(self, telegram_user_id: int, pair: str) -> bool:
        """Check if any token in a pair (e.g. 'ETH/USDT') is muted by the user."""
        muted = await self.get_muted_tokens(telegram_user_id)
        if not muted:
            return False
        tokens = {t.strip().upper() for t in pair.split("/")}
        return bool(tokens & muted)
