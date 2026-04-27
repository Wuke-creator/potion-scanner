"""Pre-renewal email (3 days before billing).

Drive spec: potion-churn-prevention.docx — "3 days before renewal:
'Your Elite renews in 3 days. Here's what you caught this month.'"

Daily cron:
  1. WhopMembersDB.list_pre_renewal_due(days_before=3) returns members
     whose current_period_end falls in the 3-day window AND haven't
     been emailed for THIS billing period yet (dedupe on
     pre_renewal_sent_for_period == current_period_end).
  2. For each, schedule one ``pre_renewal`` email and mark sent.

Population of ``current_period_end`` is the responsibility of the Whop
sync (whop_email_sync.py); when it walks memberships it should call
WhopMembersDB.set_current_period_end(whop_user_id, period_end=...).
Until that's wired, this cron is a safe no-op (no rows match).

Pause-return is handled by ``pre_pause_return_email.py`` for the same
pattern (different timestamp column).
"""
from __future__ import annotations

import asyncio
import logging
import time

from src.automations.whop_members_db import WhopMembersDB
from src.email_bot.db import EmailDB, Subscriber

logger = logging.getLogger(__name__)


class PreRenewalEmail:
    """Daily cron firing the pre-renewal email at the 3-days-before-billing mark."""

    def __init__(
        self,
        whop_members_db: WhopMembersDB,
        email_db: EmailDB,
        *,
        days_before: int = 3,
        interval_hours: int = 24,
        rejoin_url: str = "https://whop.com/potion",
    ):
        self._members = whop_members_db
        self._email_db = email_db
        self._days_before = days_before
        self._interval_sec = max(60, interval_hours * 3600)
        self._rejoin_url = rejoin_url
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="pre_renewal_email")
        logger.info(
            "PreRenewalEmail started (days_before=%d, interval=%dh)",
            self._days_before, self._interval_sec // 3600,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except asyncio.TimeoutError:
            self._task.cancel()
        finally:
            self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("PreRenewalEmail cycle crashed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_sec,
                )
                return
            except asyncio.TimeoutError:
                continue

    async def run_once(self, *, now: int | None = None) -> int:
        """One cron pass. Returns count of emails scheduled."""
        ts = now if now is not None else int(time.time())
        try:
            due = await self._members.list_pre_renewal_due(
                days_before=self._days_before, now=ts,
            )
        except Exception:
            logger.exception("PreRenewalEmail: list_pre_renewal_due failed")
            return 0

        if not due:
            return 0

        scheduled = 0
        for member, period_end in due:
            try:
                await self._enroll_one(member, period_end=period_end, now=ts)
                scheduled += 1
            except Exception:
                logger.exception(
                    "PreRenewalEmail: failed to enroll %s",
                    member.email,
                )
        if scheduled:
            logger.info(
                "PreRenewalEmail: scheduled %d email(s)", scheduled,
            )
        return scheduled

    async def _enroll_one(
        self, member, *, period_end: int, now: int,
    ) -> None:
        sub = Subscriber(
            email=member.email,
            name="",
            trigger_type="pre_renewal",
            exit_reason="none",
            created_at=now,
            rejoin_url=self._rejoin_url,
        )
        await self._email_db.upsert_subscriber(sub)
        await self._email_db.schedule_one(
            email=member.email,
            sequence="pre_renewal",
            day=0,
            due_at=now,
        )
        await self._members.mark_pre_renewal_sent(
            member.whop_user_id, period_end=period_end,
        )
