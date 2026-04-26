"""Open-signals memory layer.

Tracks the most recent open signal per ``(channel_id, pair)`` so lifecycle
events ("TP1 hit", "Move SL to BE") can be enriched with the original
entry / SL / TP prices that the update post itself doesn't carry.

Why this exists:
    Image-bot channels (Pingu Charts, Mac's Calls) often post update messages
    that just say "WET Update: TP1 here, move SL to BE" with all the
    actual numbers baked into a chart image. By recording every parsed new
    signal here, we can join lifecycle events back to their origin and
    surface the missing prices in the Telegram alert without OCR-ing the
    update image at all.

Schema (single table, no joins):
    open_signals (
        channel_id     INTEGER  -- Discord source channel
        pair           TEXT     -- e.g. "WET/USDT" (always upper-cased)
        normalised_base TEXT    -- e.g. "WET" (for fuzzy lookup by symbol only)
        side           TEXT     -- "LONG" / "SHORT"
        leverage       INTEGER
        entry          REAL
        stop_loss      REAL
        tp1            REAL
        tp2            REAL
        tp3            REAL
        trade_id       INTEGER  -- nullable; ParsedSignal carries it, OCR may not
        status         TEXT     -- "open" | "tp1_hit" | "tp2_hit" | "tp3_hit"
                                  | "stopped" | "closed" | "canceled"
        opened_at      INTEGER  -- unix ts
        last_event_at  INTEGER  -- unix ts of most recent status change
        raw_message    TEXT     -- original raw Discord content for debugging
    )

There's no UNIQUE constraint on (channel_id, pair) because the same pair
can be re-opened legitimately (one trade closes, a new one opens). Lookup
always picks the most recent ``opened_at`` row whose status is still
"open"-equivalent (i.e. not stopped/closed/canceled).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import aiosqlite

logger = logging.getLogger(__name__)


# Statuses that mean "the trade is done; don't enrich future events with it".
_TERMINAL_STATUSES = {"stopped", "closed", "canceled", "all_tp_hit"}


@dataclass
class OpenSignal:
    """A previously-recorded signal that's still considered live."""

    channel_id: int
    pair: str
    normalised_base: str
    side: str | None
    leverage: int | None
    entry: float | None
    stop_loss: float | None
    tp1: float | None
    tp2: float | None
    tp3: float | None
    trade_id: int | None
    status: str
    opened_at: int
    last_event_at: int
    raw_message: str


def _normalise_base(pair: str) -> str:
    """Pull the base ticker out of a pair string and upper-case it.

    "WET/USDT" -> "WET", "ETH/USD 10x" -> "ETH", "btc" -> "BTC".
    Mirrors ``_extract_base_symbol`` in formatter.py so lookups by either
    full pair or bare symbol both work.
    """
    if not pair:
        return ""
    head = pair.split("/", 1)[0].strip()
    head = head.split()[0] if head else head
    return "".join(c for c in head if c.isalnum()).upper()


class OpenSignalsDB:
    """aiosqlite-backed open-signals tracker.

    Mirrors the connection-lifecycle pattern the rest of the codebase uses
    (lazy connect on ``open()``, single shared connection, WAL mode for
    safe concurrent reads while the router writes).
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        if self._conn is not None:
            return
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS open_signals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id      INTEGER NOT NULL,
                pair            TEXT    NOT NULL,
                normalised_base TEXT    NOT NULL,
                side            TEXT,
                leverage        INTEGER,
                entry           REAL,
                stop_loss       REAL,
                tp1             REAL,
                tp2             REAL,
                tp3             REAL,
                trade_id        INTEGER,
                status          TEXT    NOT NULL DEFAULT 'open',
                opened_at       INTEGER NOT NULL,
                last_event_at   INTEGER NOT NULL,
                raw_message     TEXT    NOT NULL DEFAULT ''
            )
            """
        )
        # Lookup indexes: latest open by channel+base, and by trade_id
        # (trade_id lookups happen on lifecycle events that carry the ID).
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_open_signals_channel_base "
            "ON open_signals (channel_id, normalised_base, opened_at DESC)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_open_signals_trade_id "
            "ON open_signals (trade_id)"
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("OpenSignalsDB not opened — call open() first")
        return self._conn

    async def record_signal(
        self,
        *,
        channel_id: int,
        pair: str,
        side: str | None,
        leverage: int | None,
        entry: float | None,
        stop_loss: float | None,
        tp1: float | None,
        tp2: float | None,
        tp3: float | None,
        trade_id: int | None,
        raw_message: str,
        opened_at: int | None = None,
    ) -> int:
        """Insert a new signal row. Returns the row id.

        Idempotent on (channel_id, trade_id): if a row already exists with
        the same trade_id and channel, the existing row's id is returned
        and no duplicate insert happens. This keeps Discord's "edit message"
        and quick-retry behaviour from polluting the table.
        """
        conn = self._require()
        now = int(opened_at if opened_at is not None else time.time())
        base = _normalise_base(pair)

        if trade_id is not None:
            cur = await conn.execute(
                "SELECT id FROM open_signals "
                "WHERE channel_id = ? AND trade_id = ? "
                "ORDER BY opened_at DESC LIMIT 1",
                (channel_id, trade_id),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is not None:
                return int(row[0])

        cur = await conn.execute(
            """
            INSERT INTO open_signals (
                channel_id, pair, normalised_base, side, leverage,
                entry, stop_loss, tp1, tp2, tp3, trade_id,
                status, opened_at, last_event_at, raw_message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
            """,
            (
                channel_id, pair.upper(), base, side, leverage,
                entry, stop_loss, tp1, tp2, tp3, trade_id,
                now, now, raw_message,
            ),
        )
        await conn.commit()
        row_id = cur.lastrowid or 0
        await cur.close()
        logger.info(
            "open_signals: recorded %s on channel %d (id=%d, trade_id=%s)",
            pair, channel_id, row_id, trade_id,
        )
        return int(row_id)

    async def find_latest_open(
        self, *, channel_id: int, pair_or_base: str,
    ) -> OpenSignal | None:
        """Return the most recently-opened, still-live signal for this
        channel+base. ``pair_or_base`` may be a full pair like "WET/USDT"
        or a bare ticker like "WET" — we normalise to the base before
        lookup, so both forms work.

        Matches across channels are deliberately NOT done. A signal posted
        in Pingu Charts and an update posted in Mac's Calls are different
        trades. Channel scoping prevents cross-feed bleed.
        """
        base = _normalise_base(pair_or_base)
        if not base:
            return None
        conn = self._require()
        terminal_clause = (
            "AND status NOT IN (" +
            ", ".join(f"'{s}'" for s in _TERMINAL_STATUSES) + ")"
        )
        cur = await conn.execute(
            f"""
            SELECT channel_id, pair, normalised_base, side, leverage,
                   entry, stop_loss, tp1, tp2, tp3, trade_id, status,
                   opened_at, last_event_at, raw_message
            FROM open_signals
            WHERE channel_id = ?
              AND normalised_base = ?
              {terminal_clause}
            ORDER BY opened_at DESC
            LIMIT 1
            """,
            (channel_id, base),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return OpenSignal(
            channel_id=int(row[0]),
            pair=row[1],
            normalised_base=row[2],
            side=row[3],
            leverage=row[4],
            entry=row[5],
            stop_loss=row[6],
            tp1=row[7],
            tp2=row[8],
            tp3=row[9],
            trade_id=row[10],
            status=row[11],
            opened_at=int(row[12]),
            last_event_at=int(row[13]),
            raw_message=row[14],
        )

    async def find_by_trade_id(
        self, *, channel_id: int, trade_id: int,
    ) -> OpenSignal | None:
        """Direct lookup by Discord trade id (when the lifecycle event
        carries one, e.g. parsed perp-bot updates). More reliable than
        symbol matching when available."""
        conn = self._require()
        cur = await conn.execute(
            """
            SELECT channel_id, pair, normalised_base, side, leverage,
                   entry, stop_loss, tp1, tp2, tp3, trade_id, status,
                   opened_at, last_event_at, raw_message
            FROM open_signals
            WHERE channel_id = ? AND trade_id = ?
            ORDER BY opened_at DESC
            LIMIT 1
            """,
            (channel_id, trade_id),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return OpenSignal(
            channel_id=int(row[0]),
            pair=row[1],
            normalised_base=row[2],
            side=row[3],
            leverage=row[4],
            entry=row[5],
            stop_loss=row[6],
            tp1=row[7],
            tp2=row[8],
            tp3=row[9],
            trade_id=row[10],
            status=row[11],
            opened_at=int(row[12]),
            last_event_at=int(row[13]),
            raw_message=row[14],
        )

    async def update_status(
        self, *, channel_id: int, pair_or_base: str, new_status: str,
        when: int | None = None,
    ) -> bool:
        """Mark the latest open signal for this channel+base as having
        moved to ``new_status``. Returns True if a row was updated.

        We update only the most-recent row to avoid retroactively closing
        older same-symbol trades that may have shared the symbol but were
        already legitimately closed.
        """
        sig = await self.find_latest_open(
            channel_id=channel_id, pair_or_base=pair_or_base,
        )
        if sig is None:
            return False
        conn = self._require()
        now = int(when if when is not None else time.time())
        await conn.execute(
            "UPDATE open_signals SET status = ?, last_event_at = ? "
            "WHERE channel_id = ? AND normalised_base = ? "
            "  AND opened_at = ?",
            (new_status, now, channel_id, sig.normalised_base, sig.opened_at),
        )
        await conn.commit()
        logger.info(
            "open_signals: %s on channel %d -> %s",
            sig.pair, channel_id, new_status,
        )
        return True

    async def cleanup_older_than(self, *, max_age_seconds: int) -> int:
        """Delete rows older than ``max_age_seconds``. Returns row count.

        Default usage: call once a day with 30-day retention to keep the
        table small. Lifecycle joins typically happen within hours of the
        original signal post, so multi-week retention is more than enough.
        """
        conn = self._require()
        cutoff = int(time.time()) - max_age_seconds
        cur = await conn.execute(
            "DELETE FROM open_signals WHERE opened_at < ?", (cutoff,),
        )
        await conn.commit()
        deleted = cur.rowcount or 0
        await cur.close()
        if deleted:
            logger.info("open_signals: pruned %d rows older than %ds",
                        deleted, max_age_seconds)
        return deleted
