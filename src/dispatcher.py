"""DM fan-out dispatcher.

Replaces the old 1→1 Broadcaster. For each alert:

  1. Enqueue the alert with its text.
  2. Worker pool pulls alerts off the queue.
  3. For each alert, the worker queries the DB for all active verified
     users (no preferences — verification is the only gate).
  4. Each user DM is scheduled as its own work item through a shared
     token bucket that caps total outbound send rate at
     ``config.dispatcher.rate_per_sec`` (default 25/sec, headroom under
     Telegram's ~30/sec global bot limit).
  5. Per-user failures are classified and handled:
       - ``Forbidden`` (user blocked / deleted bot) → mark user inactive
         in the DB so the reverify job doesn't keep trying
       - ``RetryAfter`` → sleep and retry the same user up to the retry cap
       - Other ``TelegramError`` / unexpected → log, count, continue

The dispatcher never raises from ``dispatch()`` — the caller just hands
it a formatted alert and forgets. Everything happens in background
workers so the Discord listener loop is never blocked.

Scale targets (verified via ``scripts/stress_test.py``):
  - 500 users × 1 alert  ≈ 20s fan-out
  - 1000 users × 1 alert ≈ 40s fan-out
  - 2000 users × 1 alert ≈ 80s fan-out
  - Back-to-back alerts queue behind each other (no overlap chaos)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from telegram import Bot
from telegram.error import Forbidden, RetryAfter, TelegramError

from src.config import DispatcherConfig
from src.rate_limiter import AsyncTokenBucket
from src.verification.db import VerificationDB

logger = logging.getLogger(__name__)


@dataclass
class DispatchStats:
    """Result of a single fan-out."""

    alert_id: int
    total_users: int
    sent: int = 0
    blocked: int = 0           # user had blocked the bot
    rate_limited: int = 0      # Telegram asked us to back off
    failed: int = 0            # other errors
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None

    @property
    def duration_sec(self) -> float:
        if self.finished_at is None:
            return time.monotonic() - self.started_at
        return self.finished_at - self.started_at

    def __str__(self) -> str:
        return (
            f"alert#{self.alert_id}: {self.sent}/{self.total_users} sent, "
            f"{self.blocked} blocked, {self.rate_limited} rate-limited, "
            f"{self.failed} failed, {self.duration_sec:.1f}s"
        )


@dataclass
class _AlertJob:
    alert_id: int
    text: str
    source_key: str            # channel key for subscription filtering
    pair: str = ""             # e.g. "ETH/USDT" for muted-token filtering
    keyboard: object = None    # InlineKeyboardMarkup (optional)


class Dispatcher:
    """Queue + worker pool that fans out alerts as DMs to all active users.

    Args:
        bot: An initialized ``telegram.Bot``.
        db: The verification DB, used to list active users per alert.
        config: Rate limit + concurrency tuning.
    """

    def __init__(
        self,
        bot: Bot,
        db: VerificationDB,
        config: DispatcherConfig,
    ):
        self._bot = bot
        self._db = db
        self._cfg = config
        self._queue: asyncio.Queue[_AlertJob] = asyncio.Queue(
            maxsize=config.queue_max_size
        )
        self._bucket = AsyncTokenBucket(
            rate_per_sec=config.rate_per_sec,
            capacity=max(int(config.rate_per_sec * 2), 10),
        )
        self._send_sem = asyncio.Semaphore(config.max_concurrent)
        self._dispatcher_task: asyncio.Task | None = None
        self._running = False
        self._alert_counter = 0
        self._last_stats: DispatchStats | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._dispatcher_task = asyncio.create_task(
            self._run_dispatcher(), name="dispatcher_loop",
        )
        logger.info(
            "Dispatcher started (rate=%.1f/s, max_concurrent=%d, queue_cap=%d)",
            self._cfg.rate_per_sec,
            self._cfg.max_concurrent,
            self._cfg.queue_max_size,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except (asyncio.CancelledError, Exception):
                pass
            self._dispatcher_task = None
        logger.info("Dispatcher stopped")

    async def dispatch(
        self, text: str, source_key: str = "", pair: str = "", keyboard=None,
    ) -> None:
        """Enqueue an alert for fan-out. Never blocks longer than queue wait."""
        self._alert_counter += 1
        job = _AlertJob(
            alert_id=self._alert_counter,
            text=text,
            source_key=source_key,
            pair=pair,
            keyboard=keyboard,
        )
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            logger.error(
                "Dispatcher queue full (cap=%d) — dropping alert #%d from %s",
                self._cfg.queue_max_size,
                job.alert_id,
                source_key,
            )
            return

    @property
    def last_stats(self) -> DispatchStats | None:
        return self._last_stats

    # ------------------------------------------------------------------

    async def _run_dispatcher(self) -> None:
        """Main loop: pop one alert, fan out, repeat.

        Alerts are processed one at a time so back-to-back alerts don't
        overlap and blow through the global rate limit.
        """
        while self._running:
            try:
                job = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                stats = await self._fan_out(job)
                self._last_stats = stats
                logger.info("Dispatch complete: %s", stats)
            except Exception:
                logger.exception(
                    "Dispatch failed for alert #%d from %s",
                    job.alert_id, job.source_key,
                )
            finally:
                self._queue.task_done()

    async def _fan_out(self, job: _AlertJob) -> DispatchStats:
        if job.source_key:
            user_ids = await self._db.list_subscribed_user_ids(job.source_key)
        else:
            user_ids = await self._db.list_active_user_ids()
        stats = DispatchStats(alert_id=job.alert_id, total_users=len(user_ids))
        logger.info(
            "Fan-out starting: alert #%d (%s) → %d subscribed user(s)",
            job.alert_id, job.source_key, len(user_ids),
        )

        if not user_ids:
            stats.finished_at = time.monotonic()
            return stats

        async def _one(user_id: int) -> None:
            async with self._send_sem:
                # Skip users who muted a token in this pair
                if job.pair:
                    if await self._db.is_token_muted(user_id, job.pair):
                        return
                await self._bucket.take(1)
                await self._send_with_retries(user_id, job.text, job.keyboard, stats)

        await asyncio.gather(
            *(_one(uid) for uid in user_ids),
            return_exceptions=False,
        )
        stats.finished_at = time.monotonic()
        return stats

    async def _send_with_retries(
        self, user_id: int, text: str, keyboard, stats: DispatchStats,
    ) -> None:
        """Send one DM, classifying failures into the stats counters."""
        attempts = 0
        max_attempts = 3
        while attempts < max_attempts:
            try:
                await asyncio.wait_for(
                    self._bot.send_message(
                        chat_id=user_id,
                        text=text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup=keyboard,
                    ),
                    timeout=self._cfg.per_send_timeout_sec,
                )
                stats.sent += 1
                return
            except Forbidden:
                # User blocked the bot or deleted the chat. Mark inactive
                # so we stop paying the cost for them on every alert.
                logger.info(
                    "User %d blocked the bot — marking inactive", user_id,
                )
                stats.blocked += 1
                try:
                    await self._db.update_after_recheck(
                        telegram_user_id=user_id, is_active=False,
                    )
                except Exception:
                    logger.exception(
                        "Failed to mark user %d inactive after block", user_id,
                    )
                return
            except RetryAfter as e:
                wait = min(float(e.retry_after), 30.0)
                logger.warning(
                    "RetryAfter for user %d: sleeping %.1fs", user_id, wait,
                )
                await asyncio.sleep(wait)
                stats.rate_limited += 1
                attempts += 1
            except asyncio.TimeoutError:
                logger.warning(
                    "Timeout sending to user %d (attempt %d)", user_id, attempts + 1,
                )
                attempts += 1
                await asyncio.sleep(1.0 + attempts)
            except TelegramError as e:
                logger.warning(
                    "TelegramError sending to user %d (attempt %d): %s",
                    user_id, attempts + 1, e,
                )
                attempts += 1
                await asyncio.sleep(1.0 + attempts)
            except Exception:
                logger.exception("Unexpected send error for user %d", user_id)
                stats.failed += 1
                return
        stats.failed += 1
