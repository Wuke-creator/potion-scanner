"""SQLite store for email subscribers + scheduled sends.

Two tables:

  subscribers:
    email PRIMARY KEY. Holds user metadata captured at enrollment
    (Whop cancellation event or inactivity trigger). Used by template
    rendering for {name} substitution and by segment-by-reason logic
    at Day 5.

  scheduled_sends:
    Append-only queue. One row per scheduled email. The worker polls
    this table, picks up rows where due_at <= now AND status='pending',
    renders + delivers them, then stamps sent_at and flips status to
    'sent' (or 'failed' with an error message).

A separate DB file (``data/email.db``) so the email subsystem can be
reset without touching user verification or analytics.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass
class Subscriber:
    email: str
    name: str
    trigger_type: str        # 'cancellation' or 'inactivity'
    exit_reason: str         # one of: too_expensive, not_using, market_slow, quality_declined, found_alternative, other, fulfillment, none
    created_at: int
    rejoin_url: str = ""     # optional per-user tracking URL


@dataclass
class ScheduledSend:
    id: int
    email: str
    sequence: str            # 'winback' or 'reengagement'
    day: int                 # 1, 3, 5, or 7
    due_at: int              # epoch seconds
    sent_at: int | None
    status: str              # pending | sent | failed | canceled
    error: str | None


# Valid exit reason codes. Keep in sync with template offer variants.
EXIT_REASONS = {
    "too_expensive",       # Offer A
    "not_using",           # Offer B
    "market_slow",         # Offer C
    "quality_declined",    # Offer D
    "found_alternative",   # Offer E
    "other",               # Offer F
    "fulfillment",         # Offer F (treated as 'other' for email copy)
    "none",                # re-engagement / inactivity: no exit reason
}


_SUBSCRIBERS_DDL = """
CREATE TABLE IF NOT EXISTS subscribers (
  email         TEXT PRIMARY KEY,
  name          TEXT NOT NULL DEFAULT '',
  trigger_type  TEXT NOT NULL,
  exit_reason   TEXT NOT NULL DEFAULT 'none',
  rejoin_url    TEXT NOT NULL DEFAULT '',
  created_at    INTEGER NOT NULL
);
"""

_SENDS_DDL = """
CREATE TABLE IF NOT EXISTS scheduled_sends (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  email      TEXT NOT NULL,
  sequence   TEXT NOT NULL,
  day        INTEGER NOT NULL,
  due_at     INTEGER NOT NULL,
  sent_at    INTEGER,
  status     TEXT NOT NULL DEFAULT 'pending',
  error      TEXT
);
"""

_SENDS_DUE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_sends_due
    ON scheduled_sends (status, due_at);
"""

_SENDS_EMAIL_INDEX = """
CREATE INDEX IF NOT EXISTS idx_sends_email
    ON scheduled_sends (email);
"""


class EmailDB:
    """Async SQLite wrapper for the email subsystem."""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(_SUBSCRIBERS_DDL)
        await self._conn.execute(_SENDS_DDL)
        await self._conn.execute(_SENDS_DUE_INDEX)
        await self._conn.execute(_SENDS_EMAIL_INDEX)
        await self._conn.commit()
        logger.info("Email DB opened at %s", self._db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---- subscribers --------------------------------------------------

    async def upsert_subscriber(self, sub: Subscriber) -> None:
        """Insert or update a subscriber row. Resets on re-enrollment."""
        assert self._conn is not None
        if sub.exit_reason not in EXIT_REASONS:
            raise ValueError(f"unknown exit_reason: {sub.exit_reason!r}")
        await self._conn.execute(
            "INSERT INTO subscribers "
            "(email, name, trigger_type, exit_reason, rejoin_url, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "  name = excluded.name, "
            "  trigger_type = excluded.trigger_type, "
            "  exit_reason = excluded.exit_reason, "
            "  rejoin_url = excluded.rejoin_url, "
            "  created_at = excluded.created_at",
            (
                sub.email, sub.name, sub.trigger_type, sub.exit_reason,
                sub.rejoin_url, sub.created_at,
            ),
        )
        await self._conn.commit()

    async def get_subscriber(self, email: str) -> Subscriber | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT email, name, trigger_type, exit_reason, rejoin_url, created_at "
            "FROM subscribers WHERE email = ?",
            (email,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return Subscriber(
            email=row[0],
            name=row[1],
            trigger_type=row[2],
            exit_reason=row[3],
            rejoin_url=row[4],
            created_at=row[5],
        )

    # ---- scheduled_sends ----------------------------------------------

    async def schedule_sequence(
        self,
        email: str,
        sequence: str,
        day_offsets: tuple[int, ...] | None = None,
        now: int | None = None,
    ) -> list[int]:
        """Queue the email sequence for a subscriber.

        Cancels any pending sends from prior sequences for the same email
        so a user who re-cancels after re-joining gets a fresh sequence
        instead of overlapping delivery.

        Per-sequence defaults (both simplified to 3 emails 2026-04-18):
          winback: day 1 (soft touch), 4 (offer), 7 (last chance)
          reengagement: day 1 (miss you), 4 (what you missed + results), 7 (personal touch)

        Returns the list of inserted send IDs.
        """
        assert self._conn is not None
        if sequence not in ("winback", "reengagement"):
            raise ValueError(f"unknown sequence: {sequence!r}")
        if day_offsets is None:
            # Both sequences now share the 1/4/7 cadence per Drive spec update.
            day_offsets = (1, 4, 7)
        now = now if now is not None else int(time.time())

        # Cancel any pending sends for this email first
        await self._conn.execute(
            "UPDATE scheduled_sends SET status='canceled' "
            "WHERE email = ? AND status = 'pending'",
            (email,),
        )

        send_ids: list[int] = []
        for day in day_offsets:
            due_at = now + day * 86400
            cursor = await self._conn.execute(
                "INSERT INTO scheduled_sends "
                "(email, sequence, day, due_at, status) "
                "VALUES (?, ?, ?, ?, 'pending')",
                (email, sequence, day, due_at),
            )
            send_ids.append(cursor.lastrowid or 0)
        await self._conn.commit()
        return send_ids

    async def schedule_one(
        self,
        email: str,
        sequence: str,
        day: int,
        due_at: int | None = None,
    ) -> int:
        """Queue a single send (used by admin test endpoint)."""
        assert self._conn is not None
        if sequence not in ("winback", "reengagement"):
            raise ValueError(f"unknown sequence: {sequence!r}")
        when = due_at if due_at is not None else int(time.time())
        cursor = await self._conn.execute(
            "INSERT INTO scheduled_sends "
            "(email, sequence, day, due_at, status) "
            "VALUES (?, ?, ?, ?, 'pending')",
            (email, sequence, day, when),
        )
        await self._conn.commit()
        return cursor.lastrowid or 0

    async def due_sends(self, now: int | None = None) -> list[ScheduledSend]:
        """Return all pending sends whose due_at has passed."""
        assert self._conn is not None
        now = now if now is not None else int(time.time())
        async with self._conn.execute(
            "SELECT id, email, sequence, day, due_at, sent_at, status, error "
            "FROM scheduled_sends "
            "WHERE status = 'pending' AND due_at <= ? "
            "ORDER BY due_at ASC "
            "LIMIT 500",
            (now,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            ScheduledSend(
                id=r[0], email=r[1], sequence=r[2], day=r[3],
                due_at=r[4], sent_at=r[5], status=r[6], error=r[7],
            )
            for r in rows
        ]

    async def mark_sent(self, send_id: int) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE scheduled_sends "
            "SET status='sent', sent_at=?, error=NULL WHERE id = ?",
            (int(time.time()), send_id),
        )
        await self._conn.commit()

    async def mark_failed(self, send_id: int, error: str) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE scheduled_sends "
            "SET status='failed', sent_at=?, error=? WHERE id = ?",
            (int(time.time()), error[:500], send_id),
        )
        await self._conn.commit()

    async def count_by_status(self) -> dict[str, int]:
        """Summary counts for /admin style endpoints."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT status, COUNT(*) FROM scheduled_sends GROUP BY status"
        ) as cursor:
            rows = await cursor.fetchall()
        return {r[0]: int(r[1]) for r in rows}
