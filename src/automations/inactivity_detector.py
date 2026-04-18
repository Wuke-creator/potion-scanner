"""Feature 2: Re-engagement inactivity trigger.

Drive spec reference: 01_Automated_Email_Sequences.docx -> Tasks 5-8 (the
re-engagement half of the sequence; "at-risk inactive members").

Daily cron:
  1. Scan the whop_members roster (every Elite member, not just
     Telegram-verified ones) for members who have both a Discord ID and email
  2. For each, compute `last_seen` = MAX(last_posted_at) across tracked
     channels from activity_db
  3. If `last_seen < now - inactivity_threshold_days`, enroll them in the
     4-email re-engagement sequence via the email bot
  4. Skip users who are already enrolled in a pending sequence (cooldown)

We intentionally source from whop_members (not verified_users) so retention
emails reach every Potion Elite member, not just those who set up the
Telegram signal bot. Requires the Whop sync to have run at least once.
"""

from __future__ import annotations

import asyncio
import logging
import time

from src.automations.activity_db import ActivityDB
from src.automations.whop_members_db import WhopMembersDB
from src.email_bot.db import EmailDB, Subscriber
from src.verification.db import VerificationDB

logger = logging.getLogger(__name__)


class InactivityDetector:
    """Background cron that enrolls inactive users in the re-engagement flow."""

    def __init__(
        self,
        activity_db: ActivityDB,
        email_db: EmailDB,
        threshold_days: int = 14,
        interval_hours: int = 24,
        cooldown_days: int = 30,
        rejoin_url: str = "https://whop.com/potion",
        whop_members_db: WhopMembersDB | None = None,
        verification_db: VerificationDB | None = None,
    ):
        """whop_members_db is the preferred audience source. If it's None or
        empty (Whop sync never ran / no API key), we fall back to
        verification_db.list_active() so the feature still fires for
        Telegram-verified users. Both paths are safe to combine."""
        self._activity_db = activity_db
        self._verification_db = verification_db
        self._whop_members_db = whop_members_db
        self._email_db = email_db
        self._threshold_sec = threshold_days * 86400
        self._interval_sec = interval_hours * 3600
        self._cooldown_sec = cooldown_days * 86400
        self._rejoin_url = rejoin_url
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="inactivity_detector")
        logger.info(
            "InactivityDetector started (threshold=%dd, interval=%dh)",
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
        self._task = None
        logger.info("InactivityDetector stopped")

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("InactivityDetector cycle crashed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_sec,
                )
                return  # stop requested
            except asyncio.TimeoutError:
                continue

    async def _gather_audience(self) -> list[tuple[str, str, str]]:
        """Return list of (discord_user_id, email, label) tuples to check.

        Primary source: whop_members (full Elite roster).
        Fallback: verified_users (Telegram subset) so the feature still works
        before the first Whop sync has completed.

        The two sources are merged on discord_user_id with whop_members
        winning ties (it's the source of truth for email).
        """
        audience: dict[str, tuple[str, str]] = {}  # discord_id -> (email, label)

        if self._whop_members_db is not None:
            rows = await self._whop_members_db.list_valid_with_discord_and_email()
            for row in rows:
                audience[row.discord_user_id] = (row.email, f"whop:{row.whop_user_id}")

        if self._verification_db is not None:
            verified = await self._verification_db.list_active()
            for user in verified:
                if user.discord_user_id and user.email and user.discord_user_id not in audience:
                    audience[user.discord_user_id] = (
                        user.email, f"tg:{user.telegram_user_id}",
                    )

        return [(did, email, label) for did, (email, label) in audience.items()]

    async def run_once(self) -> dict:
        """One detection cycle. Returns summary stats for logging / tests."""
        now = int(time.time())
        cutoff = now - self._threshold_sec

        summary = {
            "scanned": 0,
            "enrolled": 0,
            "skipped_no_email": 0,
            "skipped_cooldown": 0,
            "skipped_active": 0,
        }

        audience = await self._gather_audience()
        summary["scanned"] = len(audience)

        for discord_user_id, email, label in audience:
            if self._stop_event.is_set():
                break

            last_seen = await self._activity_db.last_seen(discord_user_id)
            if last_seen is None or last_seen >= cutoff:
                summary["skipped_active"] += 1
                continue

            if not email:
                # Should not happen given our queries only return rows with
                # email, but defend against DB drift.
                summary["skipped_no_email"] += 1
                continue

            existing = await self._email_db.get_subscriber(email)
            if existing and (now - existing.created_at) < self._cooldown_sec:
                summary["skipped_cooldown"] += 1
                continue

            sub = Subscriber(
                email=email,
                name="",
                trigger_type="inactivity",
                exit_reason="none",
                rejoin_url=self._rejoin_url,
                created_at=now,
            )
            await self._email_db.upsert_subscriber(sub)
            await self._email_db.schedule_sequence(
                email=email, sequence="reengagement",
            )
            summary["enrolled"] += 1
            logger.info(
                "Enrolled inactive user %s (%s, last seen %d days ago)",
                email, label, (now - last_seen) // 86400,
            )

        logger.info("InactivityDetector cycle: %s", summary)
        return summary
