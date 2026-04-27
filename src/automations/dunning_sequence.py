"""Failed-payment dunning sequence (Day 0 / 3 / 10).

Drive spec: potion-churn-prevention.docx — "Failed-payment dunning."
Day 7 in the spec is a Discord Concierge ping (out of scope here, would
need separate Discord-message automation).

Trigger flow:

    Whop fires a payment_failed webhook
              │
              ▼
    /webhook/whop/payment-failed (src/email_bot/webhook.py)
        - HMAC-verifies via WHOP_WEBHOOK_SECRET
        - Resolves whop_user_id from the payload
        - WhopMembersDB.start_dunning(whop_user_id)
              │
              ▼
    DunningSequence cron (this module)
        - Daily walk: members with dunning_active=1
        - For each (day-offset, member) where now - dunning_started_at >= N
          AND dunning_last_day_sent < N:
            * EmailDB.schedule_one(sequence='dunning', day=N, due_at=now)
            * mark_dunning_day_sent(member, day=N)

When the member's payment succeeds (Whop fires `payment_succeeded` or the
member shows up in the next sync as valid=1), the webhook handler calls
WhopMembersDB.stop_dunning() and the sequence stops automatically.
"""
from __future__ import annotations

import asyncio
import logging
import time

from src.automations.whop_members_db import WhopMembersDB
from src.email_bot.db import EmailDB, Subscriber

logger = logging.getLogger(__name__)


# Day 7 is a Discord ping per the Drive spec, intentionally absent here.
DUNNING_DAYS: tuple[int, ...] = (0, 3, 10)


class DunningSequence:
    """Daily cron that walks members in active dunning and queues the
    next email in their cycle when due."""

    def __init__(
        self,
        whop_members_db: WhopMembersDB,
        email_db: EmailDB,
        *,
        interval_hours: int = 24,
        rejoin_url: str = "https://whop.com/potion",
    ):
        self._members = whop_members_db
        self._email_db = email_db
        self._interval_sec = max(60, interval_hours * 3600)
        self._rejoin_url = rejoin_url
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run(), name="dunning_sequence",
        )
        logger.info(
            "DunningSequence started (interval=%dh, days=%s)",
            self._interval_sec // 3600, list(DUNNING_DAYS),
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
                logger.exception("DunningSequence cycle crashed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_sec,
                )
                return
            except asyncio.TimeoutError:
                continue

    async def run_once(self, *, now: int | None = None) -> dict[int, int]:
        """One cron pass. Returns counts per day-offset."""
        ts = now if now is not None else int(time.time())
        result: dict[int, int] = {}

        for day in DUNNING_DAYS:
            try:
                due_members = await self._members.list_dunning_due(
                    days_since_dunning_started=day, now=ts,
                )
            except Exception:
                logger.exception(
                    "DunningSequence: list_dunning_due(day=%d) failed", day,
                )
                continue

            if not due_members:
                result[day] = 0
                continue

            scheduled = 0
            for member in due_members:
                try:
                    await self._enroll_one(member, day=day, now=ts)
                    scheduled += 1
                except Exception:
                    logger.exception(
                        "DunningSequence: failed to enroll %s for day %d",
                        member.email, day,
                    )

            result[day] = scheduled
            if scheduled:
                logger.info(
                    "DunningSequence day %d: scheduled %d email(s)",
                    day, scheduled,
                )

        return result

    async def _enroll_one(
        self, member, *, day: int, now: int,
    ) -> None:
        sub = Subscriber(
            email=member.email,
            name="",
            trigger_type="dunning",
            exit_reason="none",
            created_at=now,
            rejoin_url=self._rejoin_url,
        )
        try:
            await self._email_db.upsert_subscriber(sub)
        except Exception:
            logger.exception(
                "DunningSequence: upsert_subscriber failed for %s", member.email,
            )
            return
        try:
            await self._email_db.schedule_one(
                email=member.email,
                sequence="dunning",
                day=day,
                due_at=now,
            )
        except Exception:
            logger.exception(
                "DunningSequence: schedule_one failed for %s day %d",
                member.email, day,
            )
            return
        try:
            await self._members.mark_dunning_day_sent(
                member.whop_user_id, day=day,
            )
        except Exception:
            logger.exception(
                "DunningSequence: mark_dunning_day_sent failed for %s",
                member.whop_user_id,
            )
