"""AUT-026 Targeted Save Offer Router.

Drive spec reference: 06_Offer_Copy.docx Task 24 (Variants A–F) +
Master Automation Architecture section 4.3.

Fires the moment Whop posts a ``membership.deactivated`` event with a
``cancel_option`` field. Maps the cancellation reason to one of six
save-offer variants, mints a single-use Whop promo code where the
offer is a discount/trial, and queues an immediate email containing
the offer copy + actionable CTA (promo redemption URL or pause link).

Kept separate from the standard winback enrolment so the flows can be
operated independently:

  - Save-offer email lands within ~60s of cancellation (worker poll
    interval), giving the user a one-tap option to stay BEFORE the
    Day 1 winback ever runs.
  - Standard winback (Day 1/4/7) still enrolls in parallel as a fallback
    for users who ignore the save offer.

Six offer variants (matching the Drive spec verbatim):

  A. too_expensive    -> 20% off for 3 months ($79/mo)
  B. not_using        -> 30-day Whop pause link
  C. market_slow      -> 30-day Whop pause link
  D. quality_declined -> 7 free days (100% off, 1 cycle) + top-5 calls digest
  E. found_alternative-> 7-day free trial (100% off, 1 cycle) + comparison
  F. other            -> 25% off for 2 months
  (fulfillment falls into Offer F per the existing _REASON_ALIASES)

For pause offers (B + C) we don't mint a promo code — Whop's pause
flow lives behind a self-serve URL the user clicks. For discount /
trial offers we mint via ``create_one_time_promo`` and embed the
generated code into the rejoin URL via a ``?promo=`` query param,
which the templates then render as the CTA destination.

A canceller whose reason normalises to ``"none"`` (unknown reason,
empty payload) gets NO save offer and falls through to the regular
winback flow only — that's the safe default and matches the spec's
"only fire variants for known reasons" intent.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from src.email_bot.db import EmailDB, Subscriber
from src.whop_api import create_one_time_promo

logger = logging.getLogger(__name__)


# Pause-feature feature flag. The membership pause flow is on the Potion
# roadmap but not shipped yet -- there is no real Whop pause URL to send
# users to. While the flag is OFF, variants B (``not_using``) and C
# (``market_slow``) MUST behave like the templates do: render a
# discount-based fallback ($79/mo for 3 months, same as Offer A) and
# point the rejoin URL at the promo'd Whop checkout, NOT a pause URL.
# Flip to ``"1"`` / ``"true"`` once the Whop pause flow is live and the
# pause_url constructor arg points at the real self-serve page.
#
# Read at module load time so callers can flip it via env. Tests that
# need to toggle it can importlib.reload(save_offer_router).
_PAUSE_FEATURE_ENABLED = os.environ.get(
    "PAUSE_FEATURE_ENABLED", "false",
).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class SaveOfferConfig:
    """One row of the OFFER_VARIANTS table."""
    reason: str            # Subscriber.exit_reason value (already normalised)
    label: str             # Human-readable variant tag (e.g. "Offer A")
    offer_type: str        # 'discount' | 'pause' | 'trial'
    amount_off: float = 0.0     # 1-100 (percentage). Ignored for pause.
    duration_months: int = 1    # Discount duration. Ignored for pause.
    base_code: str = ""    # Promo code prefix (e.g. STAY79). Ignored for pause.
    pause_days: int = 0    # Pause duration. Ignored for discount/trial.


# Offer table — mirrors REV-6 Task 24 verbatim. Update copy via the
# template (_save_offer_day0); update offer params (discount %, base
# code, etc.) here.
#
# Pause-feature gating: when ``PAUSE_FEATURE_ENABLED`` is FALSE (the
# default until the Whop pause flow ships), variants B (``not_using``)
# and C (``market_slow``) fall back to a discount-based offer at the
# same $79/3-month price point as Offer A. This matches the templates,
# which already render discount copy for those reasons when the flag
# is off (see ``_save_offer_day0`` in templates.py). Keeping the two
# code paths aligned is what stops the router from minting a pause
# rejoin URL that the email body then contradicts.
def _build_offer_variants() -> dict[str, SaveOfferConfig]:
    if _PAUSE_FEATURE_ENABLED:
        # Pause flow is live -> use the real pause variants.
        not_using_offer = SaveOfferConfig(
            reason="not_using",
            label="Offer B — 30 day pause",
            offer_type="pause",
            pause_days=30,
        )
        market_slow_offer = SaveOfferConfig(
            reason="market_slow",
            label="Offer C — 30 day pause",
            offer_type="pause",
            pause_days=30,
        )
    else:
        # Pause flow not shipped -> render the discount fallback so the
        # rejoin URL we mint points at a working checkout instead of
        # a non-existent pause page. Mirrors the template's flag-off
        # copy for these exit reasons.
        not_using_offer = SaveOfferConfig(
            reason="not_using",
            label="Offer B — 20% off 3 months (pause flag off)",
            offer_type="discount",
            amount_off=20.0,
            duration_months=3,
            base_code="STAY79",
        )
        market_slow_offer = SaveOfferConfig(
            reason="market_slow",
            label="Offer C — 20% off 3 months (pause flag off)",
            offer_type="discount",
            amount_off=20.0,
            duration_months=3,
            base_code="STAY79",
        )
    return {
        "too_expensive": SaveOfferConfig(
            reason="too_expensive",
            label="Offer A — 20% off 3 months",
            offer_type="discount",
            amount_off=20.0,
            duration_months=3,
            base_code="STAY79",
        ),
        "not_using": not_using_offer,
        "market_slow": market_slow_offer,
        "quality_declined": SaveOfferConfig(
            reason="quality_declined",
            label="Offer D — 7 free days + top calls",
            offer_type="trial",
            amount_off=100.0,
            duration_months=1,
            base_code="QUALITY7",
        ),
        "found_alternative": SaveOfferConfig(
            reason="found_alternative",
            label="Offer E — 7 day free trial + comparison",
            offer_type="trial",
            amount_off=100.0,
            duration_months=1,
            base_code="COMPARE7",
        ),
        "other": SaveOfferConfig(
            reason="other",
            label="Offer F — 25% off 2 months",
            offer_type="discount",
            amount_off=25.0,
            duration_months=2,
            base_code="STAY25",
        ),
        "fulfillment": SaveOfferConfig(
            reason="fulfillment",
            label="Offer F — 25% off 2 months",
            offer_type="discount",
            amount_off=25.0,
            duration_months=2,
            base_code="STAY25",
        ),
        # 'none' deliberately absent: unknown reason -> no targeted save
        # offer, fall through to standard winback only.
    }


OFFER_VARIANTS: dict[str, SaveOfferConfig] = _build_offer_variants()



@dataclass
class RouteResult:
    """Outcome of one route_offer() call. Mostly for logging + tests."""
    routed: bool = False           # True if we matched a variant
    variant_label: str = ""
    offer_type: str = ""
    promo_code: Optional[str] = None
    rejoin_url: str = ""
    scheduled_send_id: int = 0


class SaveOfferRouter:
    """Mints the right Whop promo for a cancellation reason and queues
    the matching save-offer email.

    Designed to be called from the Whop ``membership.deactivated`` webhook
    handler. The handler still enrols the canceller in the regular winback
    sequence in parallel — a save-offer click should cancel the cancellation
    on Whop's side, which fires ``membership.activated`` and lets the
    reactivation handler clear pending winback sends.

    Construction kwargs:
      ``email_db``        — required, the same EmailDB the worker drains
      ``promo_api_key``   — Whop promo-creation key (separate from member-read)
      ``whop_company_id`` — the Potion Alpha company id
      ``rejoin_url_base`` — landing page that accepts a ?promo=<code> query
      ``pause_url``       — Whop self-serve URL the pause offers send to
      ``promo_ttl_days``  — promo expiry from now; 0 disables expiry
    """

    def __init__(
        self,
        email_db: EmailDB,
        promo_api_key: str,
        whop_company_id: str,
        *,
        rejoin_url_base: str = "https://whop.com/potion",
        pause_url: str = "https://whop.com/orders",
        promo_ttl_days: int = 14,
    ):
        self._db = email_db
        self._promo_api_key = promo_api_key
        self._whop_company_id = whop_company_id
        self._rejoin_url_base = rejoin_url_base
        self._pause_url = pause_url
        self._promo_ttl_days = promo_ttl_days

    @property
    def is_promo_configured(self) -> bool:
        """Whether discount/trial offers can actually mint a promo. False
        means we still send the save-offer email but the CTA falls back
        to the bare rejoin URL — recipients can still rejoin, just at
        full price. Better than dropping the email entirely."""
        return bool(self._promo_api_key and self._whop_company_id)

    async def route_offer(
        self,
        email: str,
        name: str,
        reason: str,
        discord_user_id: str = "",
    ) -> RouteResult:
        """Pick the offer variant for ``reason``, mint a promo if needed,
        upsert the subscriber row with the personalised rejoin URL, and
        schedule one immediate save-offer email.

        Returns a RouteResult describing what happened. ``routed=False``
        means no variant matched (caller should fall back to default
        winback flow only).
        """
        config = OFFER_VARIANTS.get(reason)
        if config is None:
            logger.info(
                "SaveOfferRouter: no variant for reason=%r — skipping (caller "
                "should still enrol in standard winback)", reason,
            )
            return RouteResult(routed=False)

        promo_code: Optional[str] = None
        rejoin_url = self._rejoin_url_base

        if config.offer_type in ("discount", "trial"):
            # Promo codes, coded links, and discounts have been removed from
            # the save-offer emails. Discount/trial variants now send the
            # plain base rejoin URL with no ?promo= param and no minted code.
            # The email copy for these variants has been rewritten to a
            # value / pause message that carries no discount.
            _ = discord_user_id
        elif config.offer_type == "pause":
            # Defense in depth: even if the OFFER_VARIANTS table somehow
            # still has a pause variant when the flag is off (manual
            # patch, stale import, test fixture), we refuse to mint a
            # rejoin URL pointing at a non-existent pause page. The
            # router falls back to the bare rejoin URL so the email
            # body still renders cleanly via the discount-fallback
            # copy in the template. Keep this in sync with
            # ``_PAUSE_FEATURE_ENABLED`` in templates.py.
            if not _PAUSE_FEATURE_ENABLED:
                logger.warning(
                    "SaveOfferRouter: pause offer reached route_offer for "
                    "reason=%r while PAUSE_FEATURE_ENABLED is OFF -- "
                    "falling back to bare rejoin URL. The OFFER_VARIANTS "
                    "table should have rebuilt to the discount fallback; "
                    "investigate stale module state.",
                    reason,
                )
                rejoin_url = self._rejoin_url_base
            else:
                rejoin_url = self._pause_url

        # Upsert subscriber with the offer-specific rejoin URL so the
        # template can read it directly. Use the existing subscriber row
        # if there is one — we don't want to clobber a previously-set
        # exit reason from another flow.
        sub = Subscriber(
            email=email.lower(),
            name=name,
            trigger_type="save_offer",
            exit_reason=config.reason,
            rejoin_url=rejoin_url,
            created_at=int(time.time()),
        )
        await self._db.upsert_subscriber(sub)

        send_id = await self._db.schedule_one(
            email=sub.email,
            sequence="save_offer",
            day=0,
            due_at=int(time.time()),
        )
        logger.info(
            "SaveOfferRouter: routed %s for %s (promo=%s, send_id=%d)",
            config.label, email, promo_code or "none", send_id,
        )
        return RouteResult(
            routed=True,
            variant_label=config.label,
            offer_type=config.offer_type,
            promo_code=promo_code,
            rejoin_url=rejoin_url,
            scheduled_send_id=send_id,
        )

    def _build_promo_url(self, code: str) -> str:
        """Append ?promo=<code> (or &promo=<code>) to the configured base
        rejoin URL. Whop's checkout reads this param and pre-applies the
        discount on landing.
        """
        sep = "&" if "?" in self._rejoin_url_base else "?"
        return f"{self._rejoin_url_base}{sep}promo={code}"
