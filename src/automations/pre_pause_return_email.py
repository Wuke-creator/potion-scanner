"""Pre-pause-return email (3 days before paused membership reactivates).

Drive spec: potion-churn-prevention.docx — "3 days before reactivation:
email with 'here's what you missed' + top 3 calls."

Until Whop's pause feature is wired (Whop config + role flow), the
``whop_members.pause_ends_at`` column stays at 0 for everyone, and this
cron is a safe no-op. When pause lands, the Whop sync (or a Whop
webhook for ``membership.paused``) should populate ``pause_ends_at`` and
this cron starts firing automatically.

Same dedupe pattern as PreRenewalEmail: dedupe on
``pre_pause_return_sent_for_period == pause_ends_at`` so re-pauses
trigger fresh emails.
"""
from __future__ import annotations

import asyncio
import logging
import time

from src.automations.whop_members_db import WhopMembersDB
from src.email_bot.db import EmailDB, Subscriber

logger = logging.getLogger(__name__)


class PrePauseReturnEmail:
    """Daily cron for the pre-pause-return email."""

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
        self._task = asyncio.create_task(
            self._run(), name="pre_pause_return_email",
        )
        logger.info(
            "PrePauseReturnEmail started (days_before=%d, interval=%dh)",
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
                logger.exception("PrePauseReturnEmail cycle crashed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_sec,
                )
                return
            except asyncio.TimeoutError:
                continue

    async def run_once(self, *, now: int | None = None) -> int:
        ts = now if now is not None else int(time.time())
        try:
            due = await self._members.list_pre_pause_return_due(
                days_before=self._days_before, now=ts,
            )
        except Exception:
            logger.exception(
                "PrePauseReturnEmail: list_pre_pause_return_due failed",
            )
            return 0

        if not due:
            return 0

        scheduled = 0
        for member, pause_ends_at in due:
            try:
                sub = Subscriber(
                    email=member.email,
                    name="",
                    trigger_type="pre_pause_return",
                    exit_reason="none",
                    created_at=ts,
                    rejoin_url=self._rejoin_url,
                )
                await self._email_db.upsert_subscriber(sub)
                await self._email_db.schedule_one(
                    email=member.email,
                    sequence="pre_pause_return",
                    day=0,
                    due_at=ts,
                )
                await self._members.mark_pre_pause_return_sent(
                    member.whop_user_id, pause_ends_at=pause_ends_at,
                )
                scheduled += 1
            except Exception:
                logger.exception(
                    "PrePauseReturnEmail: failed to enroll %s",
                    member.email,
                )

        if scheduled:
            logger.info(
                "PrePauseReturnEmail: scheduled %d email(s)", scheduled,
            )
        return scheduled
