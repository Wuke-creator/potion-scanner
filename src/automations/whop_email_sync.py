"""Whop sync: populate whop_members table AND backfill verified_users.email.

Walks all Whop memberships once and writes two things:

  1. `whop_members` (every Elite member, full roster): the email audience used
     by Feature 1 email half, Feature 2 inactivity detector, and Feature 4
     channel feeler. Needed because a Potion member can pay on Whop and never
     touch the Telegram signal bot; email retention must still reach them.

  2. `verified_users.email` (Telegram-verified subset): backfills email for
     the 4 (or more) users who verified before we added the `email` OAuth
     scope. Without this they would get skipped by email-based features even
     though they're in our verified_users table.

Both writes happen in the same Whop walk to avoid double the API traffic.

Run pattern:

  * Once at startup (guarded by automations.email_sync_on_startup)
  * Every 24h thereafter (WhopEmailSyncCron)
  * On demand via the `/sync-emails` admin slash command

The task is idempotent: upsert semantics on both tables, safe to re-run.
"""

from __future__ import annotations

import asyncio
import logging
import time

from src.automations.whop_members_db import WhopMembersDB
from src.verification.db import VerificationDB
from src.whop_api import WhopAPIClient, WhopAPIError

logger = logging.getLogger(__name__)


class WhopEmailSync:
    """Walks Whop memberships once and syncs roster + backfills emails.

    Not a long-lived cron: each `run_once()` call opens a fresh Whop session,
    walks all memberships, updates both DBs, and returns stats. The
    orchestrator in main.py decides when to call it (startup + on 24h timer
    + on demand via /sync-emails).
    """

    def __init__(
        self,
        verification_db: VerificationDB,
        api_key: str,
        company_id: str,
        api_base: str = "https://api.whop.com",
        members_db: WhopMembersDB | None = None,
    ):
        self._db = verification_db
        self._members_db = members_db
        self._api_key = api_key
        self._company_id = company_id
        self._api_base = api_base

    @property
    def is_configured(self) -> bool:
        """True iff we have both a key and a company id. Loggers check this
        before each run and log-and-skip with a clear message if missing."""
        return bool(self._api_key and self._company_id)

    async def run_once(self) -> dict:
        """Do one full sync cycle. Returns summary stats for logging."""
        if not self.is_configured:
            logger.info(
                "Whop email sync skipped: api_key or company_id missing",
            )
            return {"status": "skipped", "reason": "not_configured"}

        users = await self._db.list_active()
        # Build a lookup of discord_id -> telegram_user_id for verified users
        # who lack an email. These are backfill candidates for
        # verified_users.email. Everyone already with an email is left alone.
        needs_email: dict[str, int] = {}
        for user in users:
            if user.discord_user_id and not user.email:
                needs_email[user.discord_user_id] = user.telegram_user_id

        matched = 0          # verified_users matches (discord_id hit in Whop)
        updated = 0          # verified_users.email rows actually written
        roster_seen = 0      # whop_members rows upserted (the full audience)
        roster_with_email = 0
        started_at = time.time()

        logger.info(
            "Whop sync starting: %d active verified users (%d need email "
            "backfill), whop_members table will be repopulated",
            len(users), len(needs_email),
        )

        try:
            async with WhopAPIClient(
                api_key=self._api_key,
                company_id=self._company_id,
                api_base=self._api_base,
            ) as whop:
                async for member in whop.iter_memberships():
                    # 1. Upsert into whop_members roster (the email audience).
                    #    We include invalid members too so we can later track
                    #    cancellations; downstream list_valid_* methods filter
                    #    them out.
                    if self._members_db is not None and member.user_id:
                        await self._members_db.upsert_member(
                            whop_user_id=member.user_id,
                            discord_user_id=member.discord_user_id,
                            email=member.email,
                            valid=member.valid,
                            membership_id=member.membership_id,
                        )
                        if member.valid:
                            roster_seen += 1
                            if member.email:
                                roster_with_email += 1

                    # 2. Backfill verified_users.email if this Whop member
                    #    matches a pending verified user.
                    if not member.valid:
                        continue
                    if not member.discord_user_id:
                        continue
                    telegram_user_id = needs_email.get(member.discord_user_id)
                    if telegram_user_id is None:
                        continue
                    matched += 1
                    if not member.email:
                        logger.debug(
                            "Whop sync: matched discord=%s but Whop has no email",
                            member.discord_user_id,
                        )
                        continue
                    await self._db.update_email(telegram_user_id, member.email)
                    updated += 1
                    logger.info(
                        "Whop sync: populated email for telegram_user=%d",
                        telegram_user_id,
                    )
                    del needs_email[member.discord_user_id]
        except WhopAPIError as e:
            logger.error("Whop sync aborted: %s", e)
            return {
                "status": "error",
                "error": str(e),
                "matched": matched,
                "updated": updated,
                "roster_seen": roster_seen,
                "duration_sec": round(time.time() - started_at, 1),
            }

        summary = {
            "status": "ok",
            "active_users": len(users),
            "needs_email": len(needs_email) + updated,
            "matched": matched,
            "updated": updated,
            "unmatched": len(needs_email),
            "roster_seen": roster_seen,
            "roster_with_email": roster_with_email,
            "duration_sec": round(time.time() - started_at, 1),
        }
        logger.info("Whop sync cycle: %s", summary)
        return summary


class WhopEmailSyncCron:
    """Background task that re-runs WhopEmailSync every N hours.

    Kept separate from WhopEmailSync so the sync logic can be called standalone
    (from startup + slash command) without a lifecycle / task handle.
    """

    def __init__(
        self,
        sync: WhopEmailSync,
        interval_hours: int = 24,
    ):
        self._sync = sync
        self._interval_sec = interval_hours * 3600
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="whop_email_sync")
        logger.info(
            "WhopEmailSyncCron started (interval=%dh)",
            self._interval_sec // 3600,
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
        logger.info("WhopEmailSyncCron stopped")

    async def _run(self) -> None:
        # Sleep first, then run. Startup sync is handled separately in main.py
        # so this loop only handles the recurring checks.
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_sec,
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._sync.run_once()
            except Exception:
                logger.exception("WhopEmailSync cycle crashed")
