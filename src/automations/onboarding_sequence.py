"""Onboarding email sequence (Day 0 / 3 / 5 / 7 / 30 + monthly).

Drive spec reference: potion-onboarding-plan V1.docx — designed to address
the 20.1% Week-1 retention leak and 3% monthly churn. The plan calls
onboarding 'the single biggest lever for subscription retention'.

Daily cron walks ``whop_members`` and for each (day-offset, member) pair
where:
  - first_seen_at >= ``go_live_at`` (HARD safety: never enroll members
    who joined before onboarding was switched on, otherwise the 121k+
    existing roster gets flooded with Day-0/3/5 emails on first run)
  - now - first_seen_at >= N days (member is old enough for that day)
  - onboarding_last_day_sent < N (we haven't already sent that day)
... it enrolls them in the ``onboarding`` sequence via EmailDB.schedule_one
and bumps ``onboarding_last_day_sent``. The email worker picks up the row,
renders via templates._onboard_dayN, and sends.

The ``go_live_at`` cutoff is the most important parameter here. Default
is **disabled** (``go_live_at = 0`` means the cron is a no-op) so an
unconfigured deploy can never accidentally blast historical members.
Set ``ONBOARDING_GO_LIVE_AT_EPOCH`` env var (or pass ``go_live_at`` to
the constructor) to the timestamp from which new members should start
receiving onboarding emails.

Day 30 is the last "first-month" email; Day 60+ recurs monthly via the
``_onboard_monthly`` template (kept dormant in this cron until we decide
whether to actively schedule recurring monthlies — currently the cron only
schedules Days 0/3/5/7/30 to avoid email fatigue, with the monthly digest
left as a manual broadcast if/when wanted).
"""
from __future__ import annotations

import asyncio
import logging
import time

from src.automations.whop_members_db import WhopMembersDB
from src.email_bot.db import EmailDB, Subscriber

logger = logging.getLogger(__name__)


# Day offsets the cron will schedule. Day 0 fires immediately for new
# members; subsequent days fire as members age into them. Sticking to
# the Drive spec's 5-email Week 1 + Day 30 cadence.
ONBOARDING_DAYS: tuple[int, ...] = (0, 3, 5, 7, 30)


class OnboardingSequence:
    """Daily cron that enrolls newly-onboarded members in the 5-email
    onboarding sequence, day by day."""

    def __init__(
        self,
        whop_members_db: WhopMembersDB,
        email_db: EmailDB,
        *,
        interval_hours: int = 24,
        rejoin_url: str = "https://whop.com/potion",
        go_live_at: int = 0,
    ):
        """``go_live_at`` is the epoch-seconds cutoff: members whose
        ``first_seen_at < go_live_at`` are NEVER enrolled in onboarding.
        Default 0 means the cron is a no-op (safe by default).
        """
        self._members = whop_members_db
        self._email_db = email_db
        self._interval_sec = max(60, interval_hours * 3600)
        self._rejoin_url = rejoin_url
        self._go_live_at = int(go_live_at)
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        if self._go_live_at <= 0:
            logger.warning(
                "OnboardingSequence NOT started: go_live_at is unset. "
                "Set ONBOARDING_GO_LIVE_AT_EPOCH env var to enable. "
                "(This guard prevents the 121k+ existing whop_members "
                "roster from being retroactively blasted with Day 0/3/5 "
                "onboarding emails on first run.)"
            )
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run(), name="onboarding_sequence",
        )
        logger.info(
            "OnboardingSequence started (interval=%dh, days=%s, "
            "go_live_at=%d)",
            self._interval_sec // 3600, list(ONBOARDING_DAYS),
            self._go_live_at,
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
                logger.exception("OnboardingSequence cycle crashed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_sec,
                )
                return
            except asyncio.TimeoutError:
                continue

    async def run_once(self, *, now: int | None = None) -> dict[int, int]:
        """One cron pass. For each day offset, find eligible members and
        schedule the email. Returns a dict mapping day-offset -> count.

        Idempotent: if the cron runs twice in a window, the second pass
        finds zero eligible members for already-sent days because
        ``onboarding_last_day_sent`` is updated atomically per member.

        Hard guard: members with ``first_seen_at < go_live_at`` are
        filtered out at the in-Python layer regardless of what the SQL
        returns, so a stale go_live_at can never accidentally onboard
        the historical roster.
        """
        if self._go_live_at <= 0:
            # Defensive: should never reach here because start() refuses
            # to spawn the task with an unset cutoff, but if a caller
            # invokes run_once() directly we still no-op.
            return {d: 0 for d in ONBOARDING_DAYS}

        ts = now if now is not None else int(time.time())
        result: dict[int, int] = {}

        for day in ONBOARDING_DAYS:
            try:
                due_members = await self._members.list_onboarding_due(
                    days_since_first_seen=day, now=ts,
                )
            except Exception:
                logger.exception(
                    "OnboardingSequence: list_onboarding_due(day=%d) failed", day,
                )
                continue

            # Hard cutoff: drop members who joined before go-live. We
            # do this in Python (not SQL) to keep the SQL surface
            # narrow and so the cutoff is auditable in code review.
            due_members = [
                m for m in due_members
                if (getattr(m, "first_seen_at", 0) or 0) >= self._go_live_at
            ]

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
                        "OnboardingSequence: failed to enroll %s for day %d",
                        member.email, day,
                    )

            result[day] = scheduled
            if scheduled:
                logger.info(
                    "OnboardingSequence day %d: scheduled %d email(s)",
                    day, scheduled,
                )

        return result

    async def _enroll_one(
        self, member, *, day: int, now: int,
    ) -> None:
        """Subscribe + schedule + mark-sent for one member-day pair."""
        # Upsert subscriber row in email_db so the worker can render with
        # the right name / rejoin_url. trigger_type 'onboarding' is new
        # but the Subscriber dataclass accepts any string.
        sub = Subscriber(
            email=member.email,
            name="",  # Whop sync doesn't populate name; templates fall back to "there"
            trigger_type="onboarding",
            exit_reason="none",
            created_at=now,
            rejoin_url=self._rejoin_url,
        )
        try:
            await self._email_db.upsert_subscriber(sub)
        except Exception:
            logger.exception(
                "OnboardingSequence: upsert_subscriber failed for %s",
                member.email,
            )
            return

        # Queue the day's email for immediate delivery (the worker polls
        # every minute and the day offset is enforced by THIS cron's
        # eligibility filter, not by scheduled_sends.due_at).
        try:
            await self._email_db.schedule_one(
                email=member.email,
                sequence="onboarding",
                day=day,
                due_at=now,
            )
        except Exception:
            logger.exception(
                "OnboardingSequence: schedule_one failed for %s day %d",
                member.email, day,
            )
            return

        # Bump dedupe state ONLY after successful scheduling so a crash
        # mid-cycle doesn't leave a member unable to receive their day's
        # email on the next run.
        try:
            await self._members.mark_onboarding_day_sent(
                member.whop_user_id, day=day,
            )
        except Exception:
            logger.exception(
                "OnboardingSequence: mark_onboarding_day_sent failed for %s",
                member.whop_user_id,
            )
