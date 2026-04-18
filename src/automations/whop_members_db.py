"""SQLite store for the full Whop Elite member roster.

Separate from `verified_users` (which only contains Telegram-verified users)
because email-based retention automations must reach every Potion Elite
member, not just the subset who also set up the Telegram signal bot. The
table is populated by the Whop API sync cron and read by:

  - Feature 1 (Launch broadcast) email half: every member with an email
  - Feature 2 (Inactivity detector): every member with discord_user_id, for
    activity lookup in activity_db, then email send if inactive
  - Feature 4 (Channel feeler): every member with an email

The table is NOT used for:

  - Telegram DMs (we don't have telegram_user_id for non-verified members)
  - Signal forwarding (same reason)
  - `/settings` subscription toggles (verified_users only)

Data flow: Whop API -> WhopEmailSync.run_once() -> upsert_member() here.
Downstream consumers read via list_valid_with_email() or
list_valid_with_discord_and_email() for activity-joined features.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass
class WhopMemberRow:
    """One row of the whop_members table, flattened for in-Python use."""

    whop_user_id: str
    discord_user_id: str
    email: str
    valid: bool
    membership_id: str
    first_seen_at: int
    last_synced_at: int


_DDL = """
CREATE TABLE IF NOT EXISTS whop_members (
  whop_user_id     TEXT PRIMARY KEY,
  discord_user_id  TEXT NOT NULL DEFAULT '',
  email            TEXT NOT NULL DEFAULT '',
  valid            INTEGER NOT NULL DEFAULT 1,
  membership_id    TEXT NOT NULL DEFAULT '',
  first_seen_at    INTEGER NOT NULL,
  last_synced_at   INTEGER NOT NULL
);
"""

_INDEX_DISCORD = (
    "CREATE INDEX IF NOT EXISTS idx_whop_discord "
    "ON whop_members(discord_user_id)"
)
_INDEX_VALID_EMAIL = (
    "CREATE INDEX IF NOT EXISTS idx_whop_valid_email "
    "ON whop_members(valid, email)"
)


class WhopMembersDB:
    """aiosqlite wrapper for the Whop member roster.

    One connection, opened via open(), closed via close(). All queries are
    async. Safe to call from multiple coroutines because aiosqlite
    serializes writes internally.
    """

    def __init__(self, db_path: str = "data/whop_members.db"):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        """Open DB + create schema + create indices. Idempotent."""
        if self._conn is not None:
            return
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(_DDL)
        await self._conn.execute(_INDEX_DISCORD)
        await self._conn.execute(_INDEX_VALID_EMAIL)
        await self._conn.commit()
        logger.info("Whop members DB opened at %s", self._db_path)

    async def close(self) -> None:
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None

    async def upsert_member(
        self,
        whop_user_id: str,
        *,
        discord_user_id: str,
        email: str,
        valid: bool,
        membership_id: str,
        when: int | None = None,
    ) -> None:
        """Insert new row or update existing one in-place.

        `first_seen_at` is preserved on update so we can tell how long a
        member has been in the roster. `last_synced_at` always bumps to
        `when` (default: now).
        """
        assert self._conn is not None, "call open() first"
        now = when if when is not None else int(time.time())
        await self._conn.execute(
            "INSERT INTO whop_members "
            "(whop_user_id, discord_user_id, email, valid, membership_id, "
            " first_seen_at, last_synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(whop_user_id) DO UPDATE SET "
            "  discord_user_id = excluded.discord_user_id, "
            "  email = excluded.email, "
            "  valid = excluded.valid, "
            "  membership_id = excluded.membership_id, "
            "  last_synced_at = excluded.last_synced_at",
            (whop_user_id, discord_user_id, email, 1 if valid else 0,
             membership_id, now, now),
        )
        await self._conn.commit()

    async def mark_invalid(self, whop_user_id: str) -> None:
        """Flip valid=0 without touching anything else. For cancellations."""
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE whop_members SET valid = 0, last_synced_at = ? "
            "WHERE whop_user_id = ?",
            (int(time.time()), whop_user_id),
        )
        await self._conn.commit()

    async def get_by_discord(self, discord_user_id: str) -> WhopMemberRow | None:
        """Fetch one member by Discord ID. Returns None if not found."""
        assert self._conn is not None
        if not discord_user_id:
            return None
        async with self._conn.execute(
            "SELECT whop_user_id, discord_user_id, email, valid, "
            "       membership_id, first_seen_at, last_synced_at "
            "FROM whop_members WHERE discord_user_id = ? LIMIT 1",
            (discord_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return WhopMemberRow(
            whop_user_id=row[0],
            discord_user_id=row[1],
            email=row[2],
            valid=bool(row[3]),
            membership_id=row[4],
            first_seen_at=row[5],
            last_synced_at=row[6],
        )

    async def list_valid_with_email(self) -> list[WhopMemberRow]:
        """Every valid member who has an email. Used by Feature 1 email half
        and Feature 4 channel feeler."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT whop_user_id, discord_user_id, email, valid, "
            "       membership_id, first_seen_at, last_synced_at "
            "FROM whop_members WHERE valid = 1 AND email != '' "
            "ORDER BY last_synced_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            WhopMemberRow(
                whop_user_id=r[0], discord_user_id=r[1], email=r[2],
                valid=bool(r[3]), membership_id=r[4],
                first_seen_at=r[5], last_synced_at=r[6],
            )
            for r in rows
        ]

    async def list_valid_with_discord_and_email(self) -> list[WhopMemberRow]:
        """Every valid member who has both a Discord link and an email. Used
        by Feature 2 inactivity detector (needs Discord ID for the activity
        lookup + email for the send)."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT whop_user_id, discord_user_id, email, valid, "
            "       membership_id, first_seen_at, last_synced_at "
            "FROM whop_members "
            "WHERE valid = 1 AND email != '' AND discord_user_id != '' "
            "ORDER BY last_synced_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            WhopMemberRow(
                whop_user_id=r[0], discord_user_id=r[1], email=r[2],
                valid=bool(r[3]), membership_id=r[4],
                first_seen_at=r[5], last_synced_at=r[6],
            )
            for r in rows
        ]

    async def count_valid(self) -> int:
        """Small helper for the /sync-emails status output."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COUNT(*) FROM whop_members WHERE valid = 1"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def count_with_email(self) -> int:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COUNT(*) FROM whop_members WHERE valid = 1 AND email != ''"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0
