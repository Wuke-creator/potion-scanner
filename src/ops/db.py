"""SQLite store for ops captures (tickets, general/alpha messages, leadership mentions).

Three tables in a single ops.db file:

  tickets:
    Every captured message in a configured ops channel (general, alpha,
    or a thread under the need-support forum). Status fields are mutated
    by the dashboard when staff click resolve/in-progress.

  leadership_mentions:
    Subset of captured messages: the ones that @mention a senior-staff
    Discord user ID. Carries enough context to render the message inline
    in the leadership panel without joining back to tickets.

  staff_activity_daily:
    Per-day rollup of (staff_user_id, channel_id) message counts. Updated
    incrementally as the bot captures messages. The dashboard reads this
    to render the staff performance panel without scanning the full
    tickets table.

The dashboard owns the ``status`` and ``staff_notes`` columns on tickets.
The bot owns everything else. Two writers on the same DB is fine because
they touch disjoint columns and SQLite WAL mode handles concurrency.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


_TICKETS_DDL = """
CREATE TABLE IF NOT EXISTS tickets (
  message_id        INTEGER PRIMARY KEY,
  channel_id        INTEGER NOT NULL,
  channel_name      TEXT NOT NULL,
  parent_id         INTEGER,             -- forum parent for thread tickets, NULL for plain channels
  thread_name       TEXT,                -- thread title (e.g. "Ticket-username-8574"), NULL for non-thread
  source            TEXT NOT NULL,       -- 'general' | 'alpha' | 'ticket'
  author_id         TEXT NOT NULL,
  author_name       TEXT NOT NULL,
  author_is_staff   INTEGER DEFAULT 0,   -- 1 if author_id in senior staff list
  content           TEXT NOT NULL,
  created_at        INTEGER NOT NULL,    -- epoch seconds (Discord message timestamp)
  captured_at       INTEGER NOT NULL,    -- epoch seconds (when bot saw it)
  status            TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'in_progress' | 'resolved'
  staff_notes       TEXT NOT NULL DEFAULT '',
  last_action_at    INTEGER,             -- epoch when staff last touched it via dashboard
  jump_url          TEXT NOT NULL DEFAULT ''
);
"""

_TICKETS_STATUS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_tickets_status_created
  ON tickets (status, created_at DESC);
"""

_TICKETS_CHANNEL_INDEX = """
CREATE INDEX IF NOT EXISTS idx_tickets_channel_created
  ON tickets (channel_id, created_at DESC);
"""

_TICKETS_PARENT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_tickets_parent_created
  ON tickets (parent_id, created_at DESC);
"""

_TICKETS_AUTHOR_INDEX = """
CREATE INDEX IF NOT EXISTS idx_tickets_author_created
  ON tickets (author_id, created_at DESC);
"""

_LEADERSHIP_DDL = """
CREATE TABLE IF NOT EXISTS leadership_mentions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id      INTEGER NOT NULL,
  channel_id      INTEGER NOT NULL,
  channel_name    TEXT NOT NULL,
  parent_id       INTEGER,
  thread_name     TEXT,
  mentioned_id    TEXT NOT NULL,
  mentioned_name  TEXT NOT NULL,
  author_id       TEXT NOT NULL,
  author_name     TEXT NOT NULL,
  content         TEXT NOT NULL,
  created_at      INTEGER NOT NULL,
  captured_at     INTEGER NOT NULL,
  jump_url        TEXT NOT NULL DEFAULT '',
  acknowledged    INTEGER NOT NULL DEFAULT 0,
  ack_at          INTEGER
);
"""

_LEADERSHIP_RECENT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_leadership_recent
  ON leadership_mentions (acknowledged, created_at DESC);
"""

_LEADERSHIP_MENTIONED_INDEX = """
CREATE INDEX IF NOT EXISTS idx_leadership_mentioned
  ON leadership_mentions (mentioned_id, created_at DESC);
"""

_STAFF_ACTIVITY_DDL = """
CREATE TABLE IF NOT EXISTS staff_activity_daily (
  staff_user_id   TEXT NOT NULL,
  day_epoch       INTEGER NOT NULL,    -- midnight UTC epoch for the day
  channel_id      INTEGER NOT NULL,
  message_count   INTEGER NOT NULL DEFAULT 0,
  first_msg_at    INTEGER NOT NULL,
  last_msg_at     INTEGER NOT NULL,
  PRIMARY KEY (staff_user_id, day_epoch, channel_id)
);
"""

_STAFF_ACTIVITY_DAY_INDEX = """
CREATE INDEX IF NOT EXISTS idx_staff_activity_day
  ON staff_activity_daily (day_epoch, staff_user_id);
"""


def _midnight_utc(epoch_seconds: int) -> int:
    """Round an epoch timestamp down to UTC midnight."""
    return epoch_seconds - (epoch_seconds % 86400)


class OpsDB:
    """Async SQLite wrapper for ops capture.

    The bot uses ``record_ticket()``, ``record_leadership_mention()``, and
    ``bump_staff_activity()`` in append/upsert paths. The dashboard reads
    via better-sqlite3 directly and writes only ``status``/``staff_notes``
    on tickets and ``acknowledged`` on leadership mentions.
    """

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute(_TICKETS_DDL)
        await self._conn.execute(_TICKETS_STATUS_INDEX)
        await self._conn.execute(_TICKETS_CHANNEL_INDEX)
        await self._conn.execute(_TICKETS_PARENT_INDEX)
        await self._conn.execute(_TICKETS_AUTHOR_INDEX)
        await self._conn.execute(_LEADERSHIP_DDL)
        await self._conn.execute(_LEADERSHIP_RECENT_INDEX)
        await self._conn.execute(_LEADERSHIP_MENTIONED_INDEX)
        await self._conn.execute(_STAFF_ACTIVITY_DDL)
        await self._conn.execute(_STAFF_ACTIVITY_DAY_INDEX)
        await self._conn.commit()
        logger.info("Ops DB opened at %s", self._db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def record_ticket(
        self,
        message_id: int,
        channel_id: int,
        channel_name: str,
        parent_id: int | None,
        thread_name: str | None,
        source: str,
        author_id: str,
        author_name: str,
        author_is_staff: bool,
        content: str,
        created_at: int,
        jump_url: str,
    ) -> None:
        """Insert a captured message. Idempotent on message_id (INSERT OR IGNORE)
        so re-deliveries from Discord don't duplicate rows.
        """
        assert self._conn is not None
        now = int(time.time())
        await self._conn.execute(
            "INSERT OR IGNORE INTO tickets ("
            "  message_id, channel_id, channel_name, parent_id, thread_name, source,"
            "  author_id, author_name, author_is_staff, content, created_at,"
            "  captured_at, status, staff_notes, last_action_at, jump_url"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', '', NULL, ?)",
            (
                message_id, channel_id, channel_name, parent_id, thread_name, source,
                author_id, author_name, 1 if author_is_staff else 0, content,
                created_at, now, jump_url,
            ),
        )
        await self._conn.commit()

    async def record_leadership_mention(
        self,
        message_id: int,
        channel_id: int,
        channel_name: str,
        parent_id: int | None,
        thread_name: str | None,
        mentioned_id: str,
        mentioned_name: str,
        author_id: str,
        author_name: str,
        content: str,
        created_at: int,
        jump_url: str,
    ) -> None:
        assert self._conn is not None
        now = int(time.time())
        await self._conn.execute(
            "INSERT INTO leadership_mentions ("
            "  message_id, channel_id, channel_name, parent_id, thread_name,"
            "  mentioned_id, mentioned_name, author_id, author_name, content,"
            "  created_at, captured_at, jump_url, acknowledged, ack_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)",
            (
                message_id, channel_id, channel_name, parent_id, thread_name,
                mentioned_id, mentioned_name, author_id, author_name, content,
                created_at, now, jump_url,
            ),
        )
        await self._conn.commit()

    async def bump_staff_activity(
        self,
        staff_user_id: str,
        channel_id: int,
        message_at: int,
    ) -> None:
        """Increment a (staff_user, day, channel) bucket. Upsert pattern.

        Counts messages, and tracks first/last message timestamps for the day
        so the dashboard can compute "last seen" + activity windows without
        joining to tickets.
        """
        assert self._conn is not None
        day = _midnight_utc(message_at)
        await self._conn.execute(
            "INSERT INTO staff_activity_daily ("
            "  staff_user_id, day_epoch, channel_id, message_count, first_msg_at, last_msg_at"
            ") VALUES (?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(staff_user_id, day_epoch, channel_id) DO UPDATE SET "
            "  message_count = message_count + 1, "
            "  first_msg_at = MIN(first_msg_at, excluded.first_msg_at), "
            "  last_msg_at = MAX(last_msg_at, excluded.last_msg_at)",
            (staff_user_id, day, channel_id, message_at, message_at),
        )
        await self._conn.commit()
