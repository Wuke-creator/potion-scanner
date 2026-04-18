"""Whop reviews scanner.

Polls the Whop v2 reviews endpoint, stores reviews in SQLite, and relays new
ones into a Discord staff channel so senior mods see feedback instantly.

Cadence:
  - Default poll every 15 minutes
  - Each cycle fetches reviews newer than the last-seen timestamp
  - Each new review becomes one Discord message (star rating embed + text)
  - Low-star (<=3) reviews get a red embed + optional @here ping so they
    don't sit unseen in a noisy channel

Data stored (data/whop_reviews.db):
  - review_id (PK)
  - user_id, product_id, stars, title, description, created_at
  - seen_at (when our sync first saw it, used for 'new since last poll' logic)
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import aiosqlite
import discord

from src.whop_api import WhopAPIClient, WhopAPIError, WhopReview

logger = logging.getLogger(__name__)


_DDL = """
CREATE TABLE IF NOT EXISTS whop_reviews (
  review_id       TEXT PRIMARY KEY,
  user_id         TEXT NOT NULL DEFAULT '',
  product_id      TEXT NOT NULL DEFAULT '',
  stars           INTEGER NOT NULL DEFAULT 0,
  title           TEXT NOT NULL DEFAULT '',
  description     TEXT NOT NULL DEFAULT '',
  created_at      INTEGER NOT NULL,
  seen_at         INTEGER NOT NULL,
  relayed_to_discord INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reviews_created ON whop_reviews(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reviews_stars ON whop_reviews(stars);
"""


class WhopReviewsDB:
    """Thin aiosqlite wrapper for the whop_reviews table."""

    def __init__(self, db_path: str = "data/whop_reviews.db"):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        if self._conn is not None:
            return
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(_DDL)
        await self._conn.commit()
        logger.info("Whop reviews DB opened at %s", self._db_path)

    async def close(self) -> None:
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None

    async def latest_seen(self) -> int:
        """Return the max `created_at` we've stored, 0 if empty."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COALESCE(MAX(created_at), 0) FROM whop_reviews"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def exists(self, review_id: str) -> bool:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT 1 FROM whop_reviews WHERE review_id = ?", (review_id,),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def insert(self, review: WhopReview) -> bool:
        """Insert a review. Returns True if inserted, False if already seen."""
        assert self._conn is not None
        now = int(time.time())
        try:
            await self._conn.execute(
                "INSERT INTO whop_reviews "
                "(review_id, user_id, product_id, stars, title, description, "
                " created_at, seen_at, relayed_to_discord) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (review.review_id, review.user_id, review.product_id,
                 review.stars, review.title, review.description,
                 review.created_at, now),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False  # review_id already stored

    async def mark_relayed(self, review_id: str) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE whop_reviews SET relayed_to_discord = 1 WHERE review_id = ?",
            (review_id,),
        )
        await self._conn.commit()

    async def count_by_stars(self) -> dict[int, int]:
        """Return {1:N, 2:N, ..., 5:N} for dashboard / stats purposes."""
        assert self._conn is not None
        result = {s: 0 for s in range(1, 6)}
        async with self._conn.execute(
            "SELECT stars, COUNT(*) FROM whop_reviews GROUP BY stars"
        ) as cursor:
            async for row in cursor:
                if 1 <= row[0] <= 5:
                    result[row[0]] = row[1]
        return result


def _star_emoji(stars: int) -> str:
    """Render star count as a row of unicode stars for Discord embed."""
    filled = "\u2605" * max(0, min(5, stars))
    empty = "\u2606" * (5 - max(0, min(5, stars)))
    return filled + empty


def _build_discord_embed(review: WhopReview) -> discord.Embed:
    """Build a Discord embed for a single review. Low-star reviews are red,
    high-star reviews are green, 4-star is amber."""
    if review.stars <= 2:
        color = 0xc23b3b  # red
    elif review.stars == 3:
        color = 0xd89c3b  # amber
    elif review.stars == 4:
        color = 0xa7c23b  # yellow-green
    else:
        color = 0x4fbb6b  # green

    title = review.title.strip() or f"{review.stars}-star review"
    embed = discord.Embed(
        title=title[:256],
        description=(review.description or "_(no comment left)_")[:4000],
        color=color,
        timestamp=None,
    )
    embed.add_field(
        name="Rating", value=f"{_star_emoji(review.stars)} ({review.stars}/5)",
        inline=True,
    )
    if review.user_id:
        embed.add_field(name="Whop user", value=f"`{review.user_id}`", inline=True)
    if review.product_id:
        embed.add_field(name="Product", value=f"`{review.product_id}`", inline=True)
    if review.created_at:
        embed.set_footer(text=f"Review created <t:{review.created_at}:R>")
    return embed


class WhopReviewsSync:
    """Background cron that pulls new Whop reviews and relays them to Discord.

    One run cycle: fetch reviews newer than last-seen, insert into DB, for
    each new one send a Discord embed to the configured staff channel.

    Channel resolution happens lazily the first time we relay so the bot has
    time to finish its Gateway handshake.
    """

    def __init__(
        self,
        db: WhopReviewsDB,
        api_key: str,
        company_id: str,
        discord_client: discord.Client,
        channel_id: int,
        api_base: str = "https://api.whop.com",
        interval_seconds: int = 900,  # 15 min default
        ping_on_low_stars: bool = False,
    ):
        self._db = db
        self._api_key = api_key
        self._company_id = company_id
        self._api_base = api_base
        self._client = discord_client
        self._channel_id = channel_id
        self._interval = interval_seconds
        self._ping_on_low_stars = ping_on_low_stars
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key and self._company_id and self._channel_id)

    async def start(self) -> None:
        if self._task is not None:
            return
        if not self.is_configured:
            logger.info(
                "WhopReviewsSync skipped: need WHOP_API_KEY + WHOP_COMPANY_ID "
                "+ WHOP_REVIEWS_CHANNEL_ID",
            )
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="whop_reviews_sync")
        logger.info(
            "WhopReviewsSync started (interval=%ds, channel_id=%d)",
            self._interval, self._channel_id,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("WhopReviewsSync cycle crashed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval,
                )
                return
            except asyncio.TimeoutError:
                continue

    async def run_once(self) -> dict:
        """Pull new reviews once and relay to Discord. Returns summary stats."""
        since = await self._db.latest_seen()
        fetched = 0
        stored = 0
        relayed = 0
        relay_failed = 0

        try:
            async with WhopAPIClient(
                api_key=self._api_key,
                company_id=self._company_id,
                api_base=self._api_base,
            ) as whop:
                async for review in whop.iter_reviews(since_epoch=since):
                    fetched += 1
                    if await self._db.insert(review):
                        stored += 1
                        try:
                            await self._relay(review)
                            await self._db.mark_relayed(review.review_id)
                            relayed += 1
                        except Exception:
                            relay_failed += 1
                            logger.exception(
                                "Failed to relay review %s to Discord",
                                review.review_id,
                            )
        except WhopAPIError as e:
            logger.error("Whop reviews sync aborted: %s", e)
            return {
                "status": "error", "error": str(e),
                "fetched": fetched, "stored": stored, "relayed": relayed,
            }

        summary = {
            "status": "ok",
            "fetched": fetched, "stored": stored,
            "relayed": relayed, "relay_failed": relay_failed,
            "since_epoch": since,
        }
        logger.info("WhopReviewsSync cycle: %s", summary)
        return summary

    async def _relay(self, review: WhopReview) -> None:
        """Send one Discord embed to the configured channel."""
        channel = self._client.get_channel(self._channel_id)
        if channel is None:
            # Fallback: try fetching (slower, but works before cache populates)
            try:
                channel = await self._client.fetch_channel(self._channel_id)
            except Exception as e:
                raise RuntimeError(
                    f"Cannot resolve Discord channel {self._channel_id}: {e}",
                )
        embed = _build_discord_embed(review)
        content = None
        if self._ping_on_low_stars and review.stars <= 2:
            content = "@here low-star review just came in"
        await channel.send(content=content, embed=embed)
