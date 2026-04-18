"""Feature 3: Monthly Value Reminder.

Drive spec reference: 04_In_App_Notifications.docx -> Task 21 (Value Reminder).

Hourly cron scans `verified_users` for anyone whose
`last_reminder_sent_at` is more than `cycle_days` ago. For each match:
  - Pull community stats from analytics.db (same source as /data)
  - Format a "Month in Review" Telegram DM with the user's name + stats
  - Send via the Telegram bot
  - Record `last_reminder_sent_at = now` so they don't get a second one
    until the next cycle

Format is Telegram DM, not email, per Drive spec ("FORMAT: Whop notification
or Discord DM"). Works even for users without email on file.

Deliberately no "first reminder on verification day 0" behavior --
new users get their first reminder ~30 days after verifying, which
coincides with their first Whop renewal cycle.
"""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import Bot
from telegram.error import Forbidden, RetryAfter, TelegramError

from src.analytics import AnalyticsDB
from src.verification.db import VerificationDB

logger = logging.getLogger(__name__)


def _build_reminder_text(
    name: str,
    calls_30d: int,
    top_pair: str,
    top_pnl_pct: float,
    active_member_count: int,
) -> str:
    greeting = name.strip() or "there"
    top_line = (
        f"Top call: +{top_pnl_pct:.0f}% on {top_pair}"
        if top_pair else "Plenty of setups fired all month"
    )
    return (
        f"\U0001f4ca *Your Potion Month in Review*\n\n"
        f"Hey {greeting}, here's what your Elite access delivered this month:\n\n"
        f"\u2022 Calls posted: *{calls_30d}*\n"
        f"\u2022 {top_line}\n"
        f"\u2022 Active members: *{active_member_count}*\n\n"
        f"The more consistently you show up, the more it tends to add up over time."
    )


class ValueReminder:
    """Background cron that sends the monthly 'Month in Review' DM."""

    def __init__(
        self,
        telegram_bot: Bot,
        verification_db: VerificationDB,
        analytics_db: AnalyticsDB,
        cycle_days: int = 30,
        interval_hours: int = 1,
        send_rate_per_sec: float = 10.0,
    ):
        self._bot = telegram_bot
        self._verification_db = verification_db
        self._analytics_db = analytics_db
        self._cycle_sec = cycle_days * 86400
        self._interval_sec = interval_hours * 3600
        self._send_interval = 1.0 / send_rate_per_sec
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="value_reminder")
        logger.info(
            "ValueReminder started (cycle=%dd, poll=%dh)",
            self._cycle_sec // 86400, self._interval_sec // 3600,
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
        logger.info("ValueReminder stopped")

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("ValueReminder cycle crashed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_sec,
                )
                return
            except asyncio.TimeoutError:
                continue

    async def run_once(self) -> dict:
        """One cycle. Returns stats."""
        now = int(time.time())
        due_before = now - self._cycle_sec

        summary = {"scanned": 0, "sent": 0, "skipped_recent": 0, "failed": 0}

        users = await self._verification_db.list_active()
        summary["scanned"] = len(users)

        # Compute stats once per cycle (same for every user)
        window = await self._analytics_db.stats_window(
            days=30, label="30d",
            channel_keys=["perp_bot", "manual_perp", "prediction"],
        )
        calls_30d = sum(
            cs.signal_count for cs in window.per_channel.values()
        )
        # Pick the single highest-PnL across all channels
        top_pnl_pct = 0.0
        top_pair = ""
        for cs in window.per_channel.values():
            if cs.top_pnl and cs.top_pnl.pnl_pct > top_pnl_pct:
                top_pnl_pct = cs.top_pnl.pnl_pct
                top_pair = cs.top_pnl.pair
        active_member_count = len(users)

        for user in users:
            if self._stop_event.is_set():
                break

            # The anchor is verified_at, not last_reminder. New users
            # wait one full cycle before their first reminder.
            anchor = max(user.last_reminder_sent_at, user.verified_at)
            if anchor > due_before:
                summary["skipped_recent"] += 1
                continue

            text = _build_reminder_text(
                name="",  # we don't capture first name separately
                calls_30d=calls_30d,
                top_pair=top_pair,
                top_pnl_pct=top_pnl_pct,
                active_member_count=active_member_count,
            )
            try:
                await self._bot.send_message(
                    chat_id=user.telegram_user_id,
                    text=text,
                    parse_mode="Markdown",
                )
                await self._verification_db.mark_reminder_sent(
                    user.telegram_user_id, when=now,
                )
                summary["sent"] += 1
            except Forbidden:
                # User blocked bot; mark reminder sent so we don't retry
                await self._verification_db.mark_reminder_sent(
                    user.telegram_user_id, when=now,
                )
                summary["failed"] += 1
            except RetryAfter as e:
                wait = min(float(e.retry_after), 30.0)
                await asyncio.sleep(wait)
                summary["failed"] += 1
            except TelegramError:
                summary["failed"] += 1
                logger.exception(
                    "Value reminder send failed for user %d",
                    user.telegram_user_id,
                )
            except Exception:
                summary["failed"] += 1
                logger.exception(
                    "Unexpected error for user %d", user.telegram_user_id,
                )

            await asyncio.sleep(self._send_interval)

        logger.info("ValueReminder cycle: %s", summary)
        return summary
