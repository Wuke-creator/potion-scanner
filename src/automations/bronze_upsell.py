"""Bronze -> Elite upsell enrolment, driven off the daily Whop sync.

Why sync-driven (not the webhook):
    An upsell is not time-critical (a free member getting day 1 within
    24h is fine). The daily WhopEmailSync already walks every membership
    and the membership row carries the product/tier, so we can decide
    enrolment AND detect upgrades from data we already pull, with no
    extra Whop API load and no dependency on webhook delivery.

Tier signal:
    `valid` does NOT distinguish free from paid (free members are valid
    too). The product id does. ``free_product_id`` is the Whop product
    for the free "Bronze" tier ("Free Discord", prod_LVAuYCd2uhi7y).
    Any OTHER product the member holds a valid membership on counts as
    "paid" — so we don't have to enumerate every paid plan.

Safety (the important part):
    Dormant unless BOTH ``free_product_id`` is set AND ``go_live_at``
    is a real epoch > 0. ``go_live_at`` is the cutoff: only members
    whose free membership was CREATED at/after that epoch are enrolled.
    Without this, the first sync run would try to enrol the entire
    existing free-signup backlog (~120k people) into a 3-email sequence
    and mint ~120k promo codes. The default (0) enrols nobody.

Usage (one walk, no extra API calls):
    bronze.observe(member)   # called per membership during the sync walk
    await bronze.finalize()  # called once after the walk completes
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from src.email_bot.db import EmailDB, Subscriber

logger = logging.getLogger(__name__)


@dataclass
class _Acc:
    """Per-email tier state accumulated across one membership walk."""

    has_valid_free: bool = False
    has_valid_paid: bool = False
    free_created_at: int = 0
    discord_user_id: str = ""


class BronzeUpsell:
    def __init__(
        self,
        email_db: EmailDB,
        free_product_id: str,
        go_live_at: int,
        rejoin_url: str,
    ):
        self._db = email_db
        self._free_product_id = (free_product_id or "").strip()
        self._go_live_at = int(go_live_at or 0)
        self._rejoin_url = rejoin_url or "https://whop.com/potion"
        self._acc: dict[str, _Acc] = {}

    @property
    def is_enabled(self) -> bool:
        """Dormant unless a free product id AND a positive go-live epoch
        are both configured. go_live_at <= 0 means "enrol nobody" — the
        backlog guard."""
        return bool(self._free_product_id) and self._go_live_at > 0

    def reset(self) -> None:
        self._acc = {}

    def observe(self, member) -> None:
        """Fold one membership row into the per-email accumulator.

        Cheap and side-effect-free; safe to call for every membership in
        the walk (including invalid ones — only valid memberships move
        the free/paid flags)."""
        if not self.is_enabled:
            return
        email = (getattr(member, "email", "") or "").strip().lower()
        if not email:
            return
        rec = self._acc.get(email)
        if rec is None:
            rec = _Acc()
            self._acc[email] = rec
        if getattr(member, "discord_user_id", "") and not rec.discord_user_id:
            rec.discord_user_id = member.discord_user_id
        if not getattr(member, "valid", False):
            return
        product = getattr(member, "product", "") or ""
        if product == self._free_product_id:
            rec.has_valid_free = True
            created = int(getattr(member, "created_at", 0) or 0)
            if created > rec.free_created_at:
                rec.free_created_at = created
        elif product:
            # Any other product they validly hold = a paid plan.
            rec.has_valid_paid = True

    async def finalize(self) -> dict:
        """Make enrol / upgrade-stop decisions for everyone seen this walk.

        - Upgrade-stop: a member already in the bronze sequence who now
          holds a valid paid plan gets their remaining bronze sends
          cancelled (so the day-5 "30% off Elite" never lands after they
          already converted).
        - Enrol: valid free, NOT paid, free membership created at/after
          go_live_at, not already a bronze subscriber.
        """
        if not self.is_enabled:
            self.reset()
            return {"status": "disabled"}

        enrolled = upgrade_stopped = skipped_paid = 0
        skipped_pre_golive = already_enrolled = errors = 0

        for email, rec in self._acc.items():
            try:
                existing = await self._db.get_subscriber(email)
            except Exception:
                existing = None
                logger.exception("BronzeUpsell: get_subscriber crashed %s", email)
            is_bronze_sub = (
                existing is not None and existing.trigger_type == "bronze"
            )

            # Upgrade-stop takes priority: if they're in the sequence and
            # have since gone paid, kill the remaining sends.
            if is_bronze_sub and rec.has_valid_paid:
                try:
                    n = await self._db.cancel_pending(email, "bronze")
                    if n:
                        upgrade_stopped += 1
                        logger.info(
                            "BronzeUpsell: %s upgraded to paid — cancelled "
                            "%d pending bronze send(s)", email, n,
                        )
                except Exception:
                    errors += 1
                    logger.exception(
                        "BronzeUpsell: cancel_pending crashed %s", email,
                    )
                continue

            if not rec.has_valid_free:
                continue
            if rec.has_valid_paid:
                skipped_paid += 1
                continue
            if rec.free_created_at < self._go_live_at:
                skipped_pre_golive += 1
                continue
            if is_bronze_sub:
                already_enrolled += 1
                continue

            try:
                await self._db.upsert_subscriber(Subscriber(
                    email=email,
                    name="",  # v2 memberships listing doesn't carry a name
                    trigger_type="bronze",
                    exit_reason="none",  # DB sentinel for non-churn sequences
                    rejoin_url=self._rejoin_url,
                    created_at=int(time.time()),
                ))
                await self._db.schedule_sequence(email=email, sequence="bronze")
                enrolled += 1
            except Exception:
                errors += 1
                logger.exception("BronzeUpsell: enrol crashed %s", email)

        summary = {
            "status": "ok",
            "seen_emails": len(self._acc),
            "enrolled": enrolled,
            "upgrade_stopped": upgrade_stopped,
            "skipped_paid": skipped_paid,
            "skipped_pre_golive": skipped_pre_golive,
            "already_enrolled": already_enrolled,
            "errors": errors,
        }
        logger.info("BronzeUpsell finalize: %s", summary)
        self.reset()
        return summary
