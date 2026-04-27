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
  last_synced_at   INTEGER NOT NULL,
  onboarding_last_day_sent  INTEGER NOT NULL DEFAULT -1,
  dunning_active            INTEGER NOT NULL DEFAULT 0,
  dunning_started_at        INTEGER NOT NULL DEFAULT 0,
  dunning_last_day_sent     INTEGER NOT NULL DEFAULT -1,
  current_period_end        INTEGER NOT NULL DEFAULT 0,
  pre_renewal_sent_for_period INTEGER NOT NULL DEFAULT 0,
  pause_ends_at             INTEGER NOT NULL DEFAULT 0,
  pre_pause_return_sent_for_period INTEGER NOT NULL DEFAULT 0,
  inactive_day10_last_sent_at INTEGER NOT NULL DEFAULT 0
);
"""

# Migrations for existing DBs that pre-date the columns above. Try-except
# ALTER on open is the standard pattern in this codebase
# (see src/verification/db.py::_VERIFIED_MIGRATIONS).
_LIFECYCLE_MIGRATIONS = (
    "ALTER TABLE whop_members ADD COLUMN onboarding_last_day_sent INTEGER NOT NULL DEFAULT -1",
    "ALTER TABLE whop_members ADD COLUMN dunning_active INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE whop_members ADD COLUMN dunning_started_at INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE whop_members ADD COLUMN dunning_last_day_sent INTEGER NOT NULL DEFAULT -1",
    "ALTER TABLE whop_members ADD COLUMN current_period_end INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE whop_members ADD COLUMN pre_renewal_sent_for_period INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE whop_members ADD COLUMN pause_ends_at INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE whop_members ADD COLUMN pre_pause_return_sent_for_period INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE whop_members ADD COLUMN inactive_day10_last_sent_at INTEGER NOT NULL DEFAULT 0",
)

_INDEX_DISCORD = (
    "CREATE INDEX IF NOT EXISTS idx_whop_discord "
    "ON whop_members(discord_user_id)"
)
_INDEX_VALID_EMAIL = (
    "CREATE INDEX IF NOT EXISTS idx_whop_valid_email "
    "ON whop_members(valid, email)"
)
_INDEX_ONBOARDING = (
    "CREATE INDEX IF NOT EXISTS idx_whop_onboarding "
    "ON whop_members(valid, email, first_seen_at)"
)
_INDEX_DUNNING = (
    "CREATE INDEX IF NOT EXISTS idx_whop_dunning "
    "ON whop_members(dunning_active, dunning_started_at) "
    "WHERE dunning_active = 1"
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
        """Open DB + create schema + create indices. Idempotent.

        Applies lifecycle column migrations via try/except ALTER so
        existing 121k+ row DBs upgrade in place without a rebuild.
        """
        if self._conn is not None:
            return
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(_DDL)
        # In-place migrations for older DB schemas. Each ALTER adds a
        # single column with a safe default; SQLite raises "duplicate
        # column" on already-migrated DBs which we swallow.
        for stmt in _LIFECYCLE_MIGRATIONS:
            try:
                await self._conn.execute(stmt)
            except aiosqlite.OperationalError as e:
                msg = str(e).lower()
                if "duplicate column" not in msg:
                    raise
        await self._conn.execute(_INDEX_DISCORD)
        await self._conn.execute(_INDEX_VALID_EMAIL)
        await self._conn.execute(_INDEX_ONBOARDING)
        await self._conn.execute(_INDEX_DUNNING)
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

    # ---- onboarding lifecycle ----------------------------------------

    async def list_onboarding_due(
        self,
        *,
        days_since_first_seen: int,
        now: int | None = None,
    ) -> list[WhopMemberRow]:
        """Members eligible for the next onboarding email at offset
        ``days_since_first_seen``.

        Filters:
          - valid = 1, email != ''
          - first_seen_at <= now - days_since_first_seen * 86400
          - onboarding_last_day_sent < days_since_first_seen
            (so we never re-send a day that's already been queued)
        """
        assert self._conn is not None
        ts = now if now is not None else int(time.time())
        cutoff = ts - days_since_first_seen * 86400
        async with self._conn.execute(
            "SELECT whop_user_id, discord_user_id, email, valid, "
            "       membership_id, first_seen_at, last_synced_at "
            "FROM whop_members "
            "WHERE valid = 1 AND email != '' "
            "  AND first_seen_at > 0 AND first_seen_at <= ? "
            "  AND onboarding_last_day_sent < ? "
            "ORDER BY first_seen_at ASC",
            (cutoff, days_since_first_seen),
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

    async def mark_onboarding_day_sent(
        self, whop_user_id: str, *, day: int,
    ) -> None:
        """Bump onboarding_last_day_sent for a member after the cron
        successfully queues their day-N onboarding email."""
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE whop_members SET onboarding_last_day_sent = ? "
            "WHERE whop_user_id = ? AND onboarding_last_day_sent < ?",
            (day, whop_user_id, day),
        )
        await self._conn.commit()

    # ---- dunning lifecycle -------------------------------------------

    async def start_dunning(
        self,
        whop_user_id: str,
        *,
        when: int | None = None,
    ) -> bool:
        """Mark a member as in active dunning (payment failed).

        Returns True if the row was modified (i.e. wasn't already in
        dunning), False if the member was already in a cycle. Idempotent
        on repeat webhook fires within the same cycle.
        """
        assert self._conn is not None
        ts = when if when is not None else int(time.time())
        cur = await self._conn.execute(
            "UPDATE whop_members "
            "SET dunning_active = 1, "
            "    dunning_started_at = ?, "
            "    dunning_last_day_sent = -1 "
            "WHERE whop_user_id = ? AND dunning_active = 0",
            (ts, whop_user_id),
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def stop_dunning(self, whop_user_id: str) -> None:
        """Mark dunning resolved (member paid or fully cancelled)."""
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE whop_members SET dunning_active = 0 "
            "WHERE whop_user_id = ?",
            (whop_user_id,),
        )
        await self._conn.commit()

    async def list_dunning_due(
        self,
        *,
        days_since_dunning_started: int,
        now: int | None = None,
    ) -> list[WhopMemberRow]:
        """Members in active dunning whose next email day is due."""
        assert self._conn is not None
        ts = now if now is not None else int(time.time())
        cutoff = ts - days_since_dunning_started * 86400
        async with self._conn.execute(
            "SELECT whop_user_id, discord_user_id, email, valid, "
            "       membership_id, first_seen_at, last_synced_at "
            "FROM whop_members "
            "WHERE dunning_active = 1 AND email != '' "
            "  AND dunning_started_at > 0 AND dunning_started_at <= ? "
            "  AND dunning_last_day_sent < ? "
            "ORDER BY dunning_started_at ASC",
            (cutoff, days_since_dunning_started),
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

    async def mark_dunning_day_sent(
        self, whop_user_id: str, *, day: int,
    ) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE whop_members SET dunning_last_day_sent = ? "
            "WHERE whop_user_id = ? AND dunning_last_day_sent < ?",
            (day, whop_user_id, day),
        )
        await self._conn.commit()

    # ---- pre-renewal lifecycle ---------------------------------------

    async def set_current_period_end(
        self, whop_user_id: str, *, period_end: int,
    ) -> None:
        """Update the next billing date for a member. Called by the Whop
        sync as it walks memberships."""
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE whop_members SET current_period_end = ? "
            "WHERE whop_user_id = ?",
            (period_end, whop_user_id),
        )
        await self._conn.commit()

    async def list_pre_renewal_due(
        self, *, days_before: int = 3, now: int | None = None,
    ) -> list[tuple[WhopMemberRow, int]]:
        """Members whose next billing falls in `days_before` days AND
        haven't been emailed for THIS billing period yet.

        Dedupe key is current_period_end itself: when the period rolls,
        a new email is allowed; until it rolls, the row's stored
        pre_renewal_sent_for_period == current_period_end blocks
        re-sending. Returns (member_row, period_end) tuples.
        """
        assert self._conn is not None
        ts = now if now is not None else int(time.time())
        # Window: members whose period_end is between now and now+days_before+1.
        # The +1d window prevents the cron missing a member if it runs
        # slightly before/after the exact 3-day mark.
        lo = ts + (days_before - 1) * 86400
        hi = ts + (days_before + 1) * 86400
        async with self._conn.execute(
            "SELECT whop_user_id, discord_user_id, email, valid, "
            "       membership_id, first_seen_at, last_synced_at, "
            "       current_period_end "
            "FROM whop_members "
            "WHERE valid = 1 AND email != '' "
            "  AND current_period_end > 0 "
            "  AND current_period_end >= ? AND current_period_end <= ? "
            "  AND (pre_renewal_sent_for_period <> current_period_end "
            "       OR pre_renewal_sent_for_period = 0)",
            (lo, hi),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            (
                WhopMemberRow(
                    whop_user_id=r[0], discord_user_id=r[1], email=r[2],
                    valid=bool(r[3]), membership_id=r[4],
                    first_seen_at=r[5], last_synced_at=r[6],
                ),
                int(r[7]),
            )
            for r in rows
        ]

    async def mark_pre_renewal_sent(
        self, whop_user_id: str, *, period_end: int,
    ) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE whop_members SET pre_renewal_sent_for_period = ? "
            "WHERE whop_user_id = ?",
            (period_end, whop_user_id),
        )
        await self._conn.commit()

    # ---- pre-pause-return (template ready, cron runs once pause is wired) ----

    async def list_pre_pause_return_due(
        self, *, days_before: int = 3, now: int | None = None,
    ) -> list[tuple[WhopMemberRow, int]]:
        """Members whose pause ends in `days_before` days AND haven't
        been emailed for THIS pause period yet."""
        assert self._conn is not None
        ts = now if now is not None else int(time.time())
        lo = ts + (days_before - 1) * 86400
        hi = ts + (days_before + 1) * 86400
        async with self._conn.execute(
            "SELECT whop_user_id, discord_user_id, email, valid, "
            "       membership_id, first_seen_at, last_synced_at, "
            "       pause_ends_at "
            "FROM whop_members "
            "WHERE email != '' AND pause_ends_at > 0 "
            "  AND pause_ends_at >= ? AND pause_ends_at <= ? "
            "  AND (pre_pause_return_sent_for_period <> pause_ends_at "
            "       OR pre_pause_return_sent_for_period = 0)",
            (lo, hi),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            (
                WhopMemberRow(
                    whop_user_id=r[0], discord_user_id=r[1], email=r[2],
                    valid=bool(r[3]), membership_id=r[4],
                    first_seen_at=r[5], last_synced_at=r[6],
                ),
                int(r[7]),
            )
            for r in rows
        ]

    async def mark_pre_pause_return_sent(
        self, whop_user_id: str, *, pause_ends_at: int,
    ) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE whop_members SET pre_pause_return_sent_for_period = ? "
            "WHERE whop_user_id = ?",
            (pause_ends_at, whop_user_id),
        )
        await self._conn.commit()

    # ---- 10-day inactivity timestamp ----------------------------------

    async def mark_inactive_day10_sent(
        self, whop_user_id: str, *, when: int | None = None,
    ) -> None:
        assert self._conn is not None
        ts = when if when is not None else int(time.time())
        await self._conn.execute(
            "UPDATE whop_members SET inactive_day10_last_sent_at = ? "
            "WHERE whop_user_id = ?",
            (ts, whop_user_id),
        )
        await self._conn.commit()
