"""Branch evaluator: pick the variant label for a (recipient, sequence, day).

This is the brain that decides which renderer variant a scheduled email
should use. The worker is not yet wired to it; until that lands, the
evaluator runs in shadow mode for testing.

Design:
    The evaluator is a thin async dispatcher with one routing function per
    sequence. Each routing function takes the same shape:

        async def _route_<sequence>(email, day, *, ctx) -> str

    Where ``ctx`` is an :class:`EvalContext` bundling the DB handles the
    routing functions might need. The dispatcher returns the chosen branch
    label (eg ``"hot"``, ``"price"``, ``"free"``). The worker maps that
    label to a renderer via ``templates._<SEQ>_BRANCH_RENDERERS``,
    falling back to the day-keyed default renderer when no variant exists
    for the chosen branch.

    All routing functions return ``"default"`` when they can't decide
    confidently. The worker treats ``"default"`` as "use the existing
    pre-A/B renderer", so a missing or mis-bucketed user just behaves the
    way they did before this module existed. That's the safety net.

Sequence axes (matching the research-backed table in the implementation
plan):

    bronze         -> hot | warm | cold      (engagement_score tier)
    pre_renewal    -> high | low             (engagement_score, >= 10 => high)
    onboarding     -> paid | free            (whop_members.valid)
    winback        -> price | value | other  (subscriber.exit_reason)
    dunning        -> long | new             (whop_members.first_seen_at; 180d cutoff)
    reengagement   -> was_hot | was_cold     (engagement_score_history at -30d)
    paid_at_risk   -> dropped | never_engaged (history vs current)
    inactive_day10 -> was_active | never_active (history at -14d)
    save_offer     -> default                 (already routed by save_offer_router.py)

The cancel-reason mapping for winback (exit_reason -> branch) is
deliberately kept narrow:

    too_expensive          -> price
    found_alternative      -> value
    quality_declined       -> value
    not_using              -> value
    market_slow            -> other
    other / fulfillment    -> other
    none                   -> default

If you want to slice winback by tenure instead of reason later, add a
``_route_winback_by_tenure`` and swap which one the dispatcher calls. The
rest of the system doesn't care.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from src.email_bot.engagement_scoring import (
    EngagementScoreDB,
    HOT_THRESHOLD,
    TIER_COLD,
    TIER_HOT,
    TIER_WARM,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Branch label constants
# ---------------------------------------------------------------------------

BRANCH_DEFAULT = "default"

# Bronze sequence reuses the tier names directly.
BRANCH_HOT = TIER_HOT
BRANCH_WARM = TIER_WARM
BRANCH_COLD = TIER_COLD

# pre_renewal
BRANCH_HIGH = "high"
BRANCH_LOW = "low"

# onboarding
BRANCH_PAID = "paid"
BRANCH_FREE = "free"

# winback (cancel reason classes)
BRANCH_PRICE = "price"
BRANCH_VALUE = "value"
BRANCH_OTHER = "other"

# dunning (tenure classes)
BRANCH_LONG = "long"
BRANCH_NEW = "new"

# reengagement / inactive_day10
BRANCH_WAS_HOT = "was_hot"
BRANCH_WAS_COLD = "was_cold"
BRANCH_WAS_ACTIVE = "was_active"
BRANCH_NEVER_ACTIVE = "never_active"

# paid_at_risk
BRANCH_DROPPED = "dropped"
BRANCH_NEVER_ENGAGED = "never_engaged"

# Cutoff between LONG-tenure and NEW-tenure dunning recipients. Six months
# is the standard SaaS marketing convention for "long-tenure" status. Tune
# if needed: the constant is intentionally a single named threshold so the
# dial is obvious.
LONG_TENURE_CUTOFF_SECONDS = 180 * 24 * 3600


# ---------------------------------------------------------------------------
# Cancel-reason mapping
# ---------------------------------------------------------------------------

# Exit-reason -> winback branch. Mirrors the canonical exit-reason set in
# ``db.py:EXIT_REASONS``. Any code that adds a new exit reason should also
# add it here, or new reasons silently fall back to "other".
_EXIT_REASON_TO_BRANCH = {
    "too_expensive":     BRANCH_PRICE,
    "found_alternative": BRANCH_VALUE,
    "quality_declined":  BRANCH_VALUE,
    "not_using":         BRANCH_VALUE,
    "market_slow":       BRANCH_OTHER,
    "other":             BRANCH_OTHER,
    "fulfillment":       BRANCH_OTHER,
    "none":              BRANCH_DEFAULT,
}


# ---------------------------------------------------------------------------
# Lookup protocols
# ---------------------------------------------------------------------------


class WhopMembersLookup(Protocol):
    """Just the slice of whop_members.db the evaluator actually needs.

    Defined as a Protocol so tests can pass an in-memory stub without
    spinning up SQLite. Production wires this to the real db module."""

    async def is_paid_member(self, email: str) -> bool: ...
    async def first_seen_at(self, email: str) -> int | None: ...


class SubscribersLookup(Protocol):
    """Slice of email.db's subscribers table needed for winback routing."""

    async def exit_reason(self, email: str) -> str | None: ...


@dataclass
class EvalContext:
    """Bundle of dependencies the routing functions read from.

    Single object so adding a new signal (eg discord activity, payment
    history) doesn't churn every routing function's signature."""

    score_db: EngagementScoreDB
    members: WhopMembersLookup | None = None
    subscribers: SubscribersLookup | None = None
    now: int | None = None  # injectable for tests; default = time.time()

    def now_ts(self) -> int:
        return int(self.now if self.now is not None else time.time())


# ---------------------------------------------------------------------------
# Routing functions (one per sequence)
# ---------------------------------------------------------------------------


async def _route_bronze(email: str, day: int, *, ctx: EvalContext) -> str:
    row = await ctx.score_db.get(email)
    if row is None:
        return BRANCH_DEFAULT
    return row.tier  # already one of hot|warm|cold


async def _route_pre_renewal(
    email: str, day: int, *, ctx: EvalContext,
) -> str:
    row = await ctx.score_db.get(email)
    if row is None:
        return BRANCH_DEFAULT
    return BRANCH_HIGH if row.score >= HOT_THRESHOLD else BRANCH_LOW


async def _route_onboarding(
    email: str, day: int, *, ctx: EvalContext,
) -> str:
    if ctx.members is None:
        return BRANCH_DEFAULT
    is_paid = await ctx.members.is_paid_member(email)
    return BRANCH_PAID if is_paid else BRANCH_FREE


async def _route_winback(
    email: str, day: int, *, ctx: EvalContext,
) -> str:
    if ctx.subscribers is None:
        return BRANCH_DEFAULT
    reason = await ctx.subscribers.exit_reason(email)
    if not reason:
        return BRANCH_DEFAULT
    return _EXIT_REASON_TO_BRANCH.get(reason, BRANCH_OTHER)


async def _route_dunning(
    email: str, day: int, *, ctx: EvalContext,
) -> str:
    if ctx.members is None:
        return BRANCH_DEFAULT
    first_seen = await ctx.members.first_seen_at(email)
    if first_seen is None:
        return BRANCH_DEFAULT
    age = ctx.now_ts() - first_seen
    return BRANCH_LONG if age >= LONG_TENURE_CUTOFF_SECONDS else BRANCH_NEW


async def _route_reengagement(
    email: str, day: int, *, ctx: EvalContext,
) -> str:
    prior = await ctx.score_db.prior_tier(email, days_ago=30)
    if prior is None:
        # Never had a score recorded -> let the worker render the default.
        return BRANCH_DEFAULT
    if prior == TIER_HOT or prior == TIER_WARM:
        return BRANCH_WAS_HOT
    return BRANCH_WAS_COLD


async def _route_inactive_day10(
    email: str, day: int, *, ctx: EvalContext,
) -> str:
    prior = await ctx.score_db.prior_tier(email, days_ago=14)
    if prior is None:
        return BRANCH_DEFAULT
    return (
        BRANCH_WAS_ACTIVE
        if prior in (TIER_HOT, TIER_WARM)
        else BRANCH_NEVER_ACTIVE
    )


async def _route_paid_at_risk(
    email: str, day: int, *, ctx: EvalContext,
) -> str:
    """Was this paid member ever hot/warm? If yes and they're cold now,
    they "dropped". If they never crossed the warm threshold, they
    "never engaged"."""
    current = await ctx.score_db.get(email)
    if current is None:
        return BRANCH_DEFAULT
    # Most recent tier that wasn't cold.
    prior_hot_or_warm = await ctx.score_db.prior_tier(email, days_ago=60)
    if prior_hot_or_warm in (TIER_HOT, TIER_WARM):
        return BRANCH_DROPPED
    return BRANCH_NEVER_ENGAGED


async def _route_save_offer(
    email: str, day: int, *, ctx: EvalContext,
) -> str:
    """save_offer is already routed by ``automations/save_offer_router.py``
    based on ``exit_reason`` -> variants A through F. The evaluator
    intentionally short-circuits to default so the existing router stays
    authoritative."""
    return BRANCH_DEFAULT


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


_RoutingFn = Callable[[str, int], Awaitable[str]]


_DISPATCH: dict[str, Callable[..., Awaitable[str]]] = {
    "bronze":         _route_bronze,
    "pre_renewal":    _route_pre_renewal,
    "onboarding":     _route_onboarding,
    "winback":        _route_winback,
    "dunning":        _route_dunning,
    "reengagement":   _route_reengagement,
    "inactive_day10": _route_inactive_day10,
    "paid_at_risk":   _route_paid_at_risk,
    "save_offer":     _route_save_offer,
}


async def pick_variant(
    email: str,
    sequence: str,
    day: int,
    *,
    ctx: EvalContext,
) -> str:
    """Return the branch label for one scheduled send.

    Always returns a string. Falls through to "default" rather than
    raising on unknown sequence names so a typo in scheduling code can
    never block a send. Logs a warning so the typo gets caught.
    """
    fn = _DISPATCH.get(sequence)
    if fn is None:
        logger.warning(
            "branch_evaluator: unknown sequence %r, falling back to default",
            sequence,
        )
        return BRANCH_DEFAULT
    try:
        branch = await fn(email, day, ctx=ctx)
    except Exception:
        # Routing must never block delivery. Any exception here is a bug
        # in the evaluator; log it loudly and let the default renderer
        # carry the send.
        logger.exception(
            "branch_evaluator: routing failed for seq=%s day=%d email=%s",
            sequence, day, email,
        )
        return BRANCH_DEFAULT
    if not branch:
        return BRANCH_DEFAULT
    return branch
