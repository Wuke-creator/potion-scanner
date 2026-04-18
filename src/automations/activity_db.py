"""Discord activity tracker — SQLite store of last-posted timestamps.

Single table:

  discord_activity(discord_user_id TEXT, channel_id INTEGER, last_posted_at INTEGER)
  PRIMARY KEY (discord_user_id, channel_id)

On every message from a configured activity-tracking channel, the
listener upserts this row. From here:

  - Feature 2 (inactivity detector) queries:
      "users whose MAX(last_posted_at) across all channels < cutoff"

  - Feature 4 (channel feeler) queries:
      "distinct users who posted in channel X in the last N days"

No user names or message content stored. Just user_id + channel_id +
last-seen epoch seconds.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


_ACTIVITY_DDL = """
CREATE TABLE IF NOT EXISTS discord_activity (
  discord_user_id  TEXT NOT NULL,
  channel_id       INTEGER NOT NULL,
  last_posted_at   INTEGER NOT NULL,
  PRIMARY KEY (discord_user_id, channel_id)
);
"""

_ACTIVITY_USER_INDEX = """
CREATE INDEX IF NOT EXISTS idx_activity_user_time
    ON discord_activity (discord_user_id, last_posted_at);
"""

_ACTIVITY_CHANNEL_INDEX = """
CREATE INDEX IF NOT EXISTS idx_activity_channel_time
    ON discord_activity (channel_id, last_posted_at);
"""

_FEELER_DDL = """
CREATE TABLE IF NOT EXISTS channel_feeler_sent (
  channel_id  INTEGER PRIMARY KEY,
  sent_at     INTEGER NOT NULL
);
"""


class ActivityDB:
    """Async SQLite wrapper for Discord post activity + feeler cooldowns."""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(_ACTIVITY_DDL)
        await self._conn.execute(_ACTIVITY_USER_INDEX)
        await self._conn.execute(_ACTIVITY_CHANNEL_INDEX)
        await self._conn.execute(_FEELER_DDL)
        await self._conn.commit()
        logger.info("Activity DB opened at %s", self._db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---- post recording ----------------------------------------------

    async def record_post(
        self, discord_user_id: str, channel_id: int, when: int | None = None,
    ) -> None:
        """Upsert the last-posted timestamp for (user, channel)."""
        assert self._conn is not None
        ts = when if when is not None else int(time.time())
        await self._conn.execute(
            "INSERT INTO discord_activity "
            "(discord_user_id, channel_id, last_posted_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(discord_user_id, channel_id) DO UPDATE SET "
            "  last_posted_at = MAX(excluded.last_posted_at, last_posted_at)",
            (discord_user_id, channel_id, ts),
        )
        await self._conn.commit()

    # ---- user-level queries (Feature 2: inactivity) ------------------

    async def last_seen(self, discord_user_id: str) -> int | None:
        """Return the epoch of the user's most recent post across all channels."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT MAX(last_posted_at) FROM discord_activity "
            "WHERE discord_user_id = ?",
            (discord_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])

    async def users_inactive_since(
        self, cutoff_epoch: int,
    ) -> list[str]:
        """Return Discord user IDs whose last post is older than cutoff.

        Only users who have posted at least once (no entry = unknown, skipped).
        The inactivity detector will cross-reference with verified_users to
        pick the right subset.
        """
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT discord_user_id "
            "FROM discord_activity "
            "GROUP BY discord_user_id "
            "HAVING MAX(last_posted_at) < ?",
            (cutoff_epoch,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [r[0] for r in rows]

    # ---- channel-level queries (Feature 4: feeler) -------------------

    async def count_unique_posters(
        self, channel_id: int, since_epoch: int,
    ) -> int:
        """How many distinct users posted in ``channel_id`` since ``since_epoch``."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COUNT(DISTINCT discord_user_id) FROM discord_activity "
            "WHERE channel_id = ? AND last_posted_at >= ?",
            (channel_id, since_epoch),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    # ---- feeler cooldown tracking ------------------------------------

    async def can_send_feeler(
        self, channel_id: int, cooldown_seconds: int,
    ) -> bool:
        """True if no feeler has been sent for this channel within cooldown."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT sent_at FROM channel_feeler_sent WHERE channel_id = ?",
            (channel_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return True
        return int(row[0]) < int(time.time()) - cooldown_seconds

    async def mark_feeler_sent(
        self, channel_id: int, when: int | None = None,
    ) -> None:
        assert self._conn is not None
        ts = when if when is not None else int(time.time())
        await self._conn.execute(
            "INSERT INTO channel_feeler_sent (channel_id, sent_at) "
            "VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET sent_at = excluded.sent_at",
            (channel_id, ts),
        )
        await self._conn.commit()
