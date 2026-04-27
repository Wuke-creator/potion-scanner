"""10-day inactivity email (one-shot, distinct from 14-day reengagement).

Drive spec: potion-churn-prevention.docx — "Trigger: Elite inactive 10+
days. Action: Email '...here's your week in Potion'."

Sits BETWEEN the 5-day Concierge Discord ping (out of scope for this
codebase) and the existing 14-day reengagement sequence:

    Day 0       member is active
    Day 5       Concierge ping (Discord, not built here)
    Day 10      THIS email — one-shot, gentle nudge
    Day 14      reengagement Day 1 (existing sequence kicks in)

Dedupe is by ``whop_members.inactive_day10_last_sent_at`` plus a 30-day
cooldown so a member who recovers and goes inactive again can re-receive
the email in a future cycle.

Implementation mirrors InactivityDetector but uses the one-shot
``inactive_day10`` sequence (single email, not a 4-email cadence) and
its own dedupe column. Reuses the activity-db lookup pattern so we don't
duplicate that code path.
"""
from __future__ import annotations

import asyncio
import logging
import time

from src.automations.activity_db import ActivityDB
from src.automations.whop_members_db import WhopMembersDB
from src.email_bot.db import EmailDB, Subscriber

logger = logging.getLogger(__name__)


class InactivityDay10Email:
    """Daily cron firing the one-shot 10-day inactivity email."""

    def __init__(
        self,
        activity_db: ActivityDB,
        whop_members_db: WhopMembersDB,
        email_db: EmailDB,
        *,
        threshold_days: int = 10,
        interval_hours: int = 24,
        cooldown_days: int = 30,
        rejoin_url: str = "https://whop.com/potion",
    ):
        self._activity_db = activity_db
        self._members = whop_members_db
        self._email_db = email_db
        self._threshold_sec = threshold_days * 86400
        self._interval_sec = max(60, interval_hours * 3600)
        self._cooldown_sec = cooldown_days * 86400
        self._rejoin_url = rejoin_url
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run(), name="inactivity_day10",
        )
        logger.info(
            "InactivityDay10Email started (threshold=%dd, interval=%dh)",
            self._threshold_sec // 86400, self._interval_sec // 3600,
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
                logger.exception("InactivityDay10Email cycle crashed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_sec,
                )
                return
            except asyncio.TimeoutError:
                continue

    async def run_once(self, *, now: int | None = None) -> dict:
        ts = now if now is not None else int(time.time())
        cutoff = ts - self._threshold_sec

        summary = {"scanned": 0, "enrolled": 0,
                   "skipped_active": 0, "skipped_cooldown": 0}

        try:
            members = await self._members.list_valid_with_discord_and_email()
        except Exception:
            logger.exception(
                "InactivityDay10Email: list_valid_with_discord_and_email failed",
            )
            return summary

        summary["scanned"] = len(members)

        for member in members:
            if self._stop_event.is_set():
                break
            try:
                last_seen = await self._activity_db.last_seen(
                    member.discord_user_id,
                )
            except Exception:
                logger.exception(
                    "InactivityDay10Email: activity_db.last_seen crashed for %s",
                    member.discord_user_id,
                )
                continue

            # Active in the last 10 days — skip.
            if last_seen is None or last_seen >= cutoff:
                summary["skipped_active"] += 1
                continue

            # Cooldown: if we've already sent a 10-day email recently,
            # don't re-fire on the same dormant member every cron cycle.
            last_sent = (
                getattr(member, "inactive_day10_last_sent_at", 0) or 0
            )
            # member object is a WhopMemberRow — doesn't carry the
            # last_sent_at field. Re-query via direct row lookup. (We
            # could expand the row dataclass, but adding a single helper
            # avoids bumping the public schema.)
            if last_sent == 0:
                last_sent = await self._fetch_last_sent_at(
                    member.whop_user_id,
                )
            if last_sent and (ts - last_sent) < self._cooldown_sec:
                summary["skipped_cooldown"] += 1
                continue

            await self._enroll_one(member, now=ts)
            summary["enrolled"] += 1

        if summary["enrolled"]:
            logger.info("InactivityDay10Email: %s", summary)
        return summary

    async def _fetch_last_sent_at(self, whop_user_id: str) -> int:
        """One-row lookup of inactive_day10_last_sent_at. Cheap; the
        column is indexed via the whop_user_id PK."""
        # We use the underlying connection directly to avoid plumbing a
        # new public method through WhopMembersDB for one read.
        conn = self._members._conn  # type: ignore[attr-defined]
        if conn is None:
            return 0
        async with conn.execute(
            "SELECT inactive_day10_last_sent_at FROM whop_members "
            "WHERE whop_user_id = ?",
            (whop_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row and row[0] else 0

    async def _enroll_one(self, member, *, now: int) -> None:
        sub = Subscriber(
            email=member.email,
            name="",
            trigger_type="inactive_day10",
            exit_reason="none",
            created_at=now,
            rejoin_url=self._rejoin_url,
        )
        try:
            await self._email_db.upsert_subscriber(sub)
            await self._email_db.schedule_one(
                email=member.email,
                sequence="inactive_day10",
                day=0,
                due_at=now,
            )
            await self._members.mark_inactive_day10_sent(
                member.whop_user_id, when=now,
            )
        except Exception:
            logger.exception(
                "InactivityDay10Email: enroll_one failed for %s", member.email,
            )
