"""Tests for branch_evaluator.pick_variant.

The routing functions are pure dispatches over a few small lookups, so
the tests use stub lookups (Protocol implementations) instead of spinning
up the real whop / subscribers DBs. The engagement_score side gets a
real EngagementScoreDB on a tmp_path file because it's already cheap and
catches realistic SQL bugs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from src.email_bot.branch_evaluator import (
    BRANCH_COLD,
    BRANCH_DEFAULT,
    BRANCH_DROPPED,
    BRANCH_FREE,
    BRANCH_HIGH,
    BRANCH_HOT,
    BRANCH_LONG,
    BRANCH_LOW,
    BRANCH_NEVER_ACTIVE,
    BRANCH_NEVER_ENGAGED,
    BRANCH_NEW,
    BRANCH_OTHER,
    BRANCH_PAID,
    BRANCH_PRICE,
    BRANCH_VALUE,
    BRANCH_WARM,
    BRANCH_WAS_ACTIVE,
    BRANCH_WAS_COLD,
    BRANCH_WAS_HOT,
    EvalContext,
    LONG_TENURE_CUTOFF_SECONDS,
    pick_variant,
)
from src.email_bot.engagement_scoring import (
    EngagementScore,
    EngagementScoreDB,
    TIER_COLD,
    TIER_HOT,
    TIER_WARM,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubMembers:
    def __init__(
        self,
        *,
        paid: set[str] | None = None,
        first_seen: dict[str, int] | None = None,
    ):
        self._paid = {e.lower() for e in (paid or set())}
        self._first_seen = {k.lower(): v for k, v in (first_seen or {}).items()}

    async def is_paid_member(self, email: str) -> bool:
        return email.lower() in self._paid

    async def first_seen_at(self, email: str) -> int | None:
        return self._first_seen.get(email.lower())


class _StubSubscribers:
    def __init__(self, *, reasons: dict[str, str] | None = None):
        self._reasons = {k.lower(): v for k, v in (reasons or {}).items()}

    async def exit_reason(self, email: str) -> str | None:
        return self._reasons.get(email.lower())


@pytest_asyncio.fixture
async def score_db(tmp_path: Path):
    db = EngagementScoreDB(db_path=str(tmp_path / "events.db"))
    await db.open()
    yield db
    await db.close()


def _ctx(
    score_db: EngagementScoreDB,
    *,
    members: _StubMembers | None = None,
    subscribers: _StubSubscribers | None = None,
    now: int = 1_700_000_000,
) -> EvalContext:
    return EvalContext(
        score_db=score_db,
        members=members,
        subscribers=subscribers,
        now=now,
    )


async def _seed_score(
    db: EngagementScoreDB, *,
    email: str, tier: str, score: int, when: int,
) -> None:
    await db.upsert(EngagementScore(
        email=email, score=score, tier=tier,
        opens_30d=0, clicks_30d=0,
        last_open_at=None, last_click_at=None,
        days_since_last_event=None,
        updated_at=when,
    ))


# ---------------------------------------------------------------------------
# bronze: tier passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBronzeRouting:
    async def test_hot_user_routes_hot(self, score_db):
        await _seed_score(
            score_db, email="hot@x.com", tier=TIER_HOT,
            score=12, when=int(time.time()),
        )
        branch = await pick_variant(
            "hot@x.com", "bronze", 3, ctx=_ctx(score_db),
        )
        assert branch == BRANCH_HOT

    async def test_warm_routes_warm(self, score_db):
        await _seed_score(
            score_db, email="warm@x.com", tier=TIER_WARM,
            score=5, when=int(time.time()),
        )
        branch = await pick_variant(
            "warm@x.com", "bronze", 5, ctx=_ctx(score_db),
        )
        assert branch == BRANCH_WARM

    async def test_no_score_falls_through_to_default(self, score_db):
        branch = await pick_variant(
            "unknown@x.com", "bronze", 3, ctx=_ctx(score_db),
        )
        assert branch == BRANCH_DEFAULT


# ---------------------------------------------------------------------------
# pre_renewal: HIGH/LOW on engagement score
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPreRenewalRouting:
    async def test_high_score_routes_high(self, score_db):
        await _seed_score(
            score_db, email="active@x.com", tier=TIER_HOT,
            score=12, when=int(time.time()),
        )
        branch = await pick_variant(
            "active@x.com", "pre_renewal", 0, ctx=_ctx(score_db),
        )
        assert branch == BRANCH_HIGH

    async def test_low_score_routes_low(self, score_db):
        await _seed_score(
            score_db, email="quiet@x.com", tier=TIER_WARM,
            score=5, when=int(time.time()),
        )
        branch = await pick_variant(
            "quiet@x.com", "pre_renewal", 0, ctx=_ctx(score_db),
        )
        assert branch == BRANCH_LOW


# ---------------------------------------------------------------------------
# onboarding: PAID/FREE on whop membership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestOnboardingRouting:
    async def test_paid_member_routes_paid(self, score_db):
        members = _StubMembers(paid={"paid@x.com"})
        ctx = _ctx(score_db, members=members)
        assert await pick_variant("paid@x.com", "onboarding", 0, ctx=ctx) == BRANCH_PAID

    async def test_non_member_routes_free(self, score_db):
        members = _StubMembers(paid={"someone-else@x.com"})
        ctx = _ctx(score_db, members=members)
        assert await pick_variant("free@x.com", "onboarding", 3, ctx=ctx) == BRANCH_FREE

    async def test_missing_members_lookup_falls_back_to_default(self, score_db):
        ctx = _ctx(score_db, members=None)
        assert await pick_variant("x@x.com", "onboarding", 0, ctx=ctx) == BRANCH_DEFAULT


# ---------------------------------------------------------------------------
# winback: exit_reason -> branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestWinbackRouting:
    async def test_too_expensive_routes_price(self, score_db):
        subs = _StubSubscribers(reasons={"u@x.com": "too_expensive"})
        ctx = _ctx(score_db, subscribers=subs)
        assert await pick_variant("u@x.com", "winback", 1, ctx=ctx) == BRANCH_PRICE

    @pytest.mark.parametrize(
        "reason",
        ["found_alternative", "quality_declined", "not_using"],
    )
    async def test_value_reasons_route_value(self, score_db, reason):
        subs = _StubSubscribers(reasons={"u@x.com": reason})
        ctx = _ctx(score_db, subscribers=subs)
        assert await pick_variant("u@x.com", "winback", 4, ctx=ctx) == BRANCH_VALUE

    @pytest.mark.parametrize("reason", ["market_slow", "other", "fulfillment"])
    async def test_other_reasons_route_other(self, score_db, reason):
        subs = _StubSubscribers(reasons={"u@x.com": reason})
        ctx = _ctx(score_db, subscribers=subs)
        assert await pick_variant("u@x.com", "winback", 7, ctx=ctx) == BRANCH_OTHER

    async def test_none_routes_default(self, score_db):
        subs = _StubSubscribers(reasons={"u@x.com": "none"})
        ctx = _ctx(score_db, subscribers=subs)
        assert await pick_variant("u@x.com", "winback", 1, ctx=ctx) == BRANCH_DEFAULT

    async def test_no_subscriber_record_falls_back_to_default(self, score_db):
        subs = _StubSubscribers(reasons={})
        ctx = _ctx(score_db, subscribers=subs)
        assert await pick_variant("u@x.com", "winback", 1, ctx=ctx) == BRANCH_DEFAULT


# ---------------------------------------------------------------------------
# dunning: LONG/NEW on tenure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDunningRouting:
    async def test_long_tenure_routes_long(self, score_db):
        now = 1_700_000_000
        members = _StubMembers(
            first_seen={"vet@x.com": now - LONG_TENURE_CUTOFF_SECONDS - 86400},
        )
        ctx = _ctx(score_db, members=members, now=now)
        assert await pick_variant("vet@x.com", "dunning", 0, ctx=ctx) == BRANCH_LONG

    async def test_new_member_routes_new(self, score_db):
        now = 1_700_000_000
        members = _StubMembers(
            first_seen={"newbie@x.com": now - 7 * 86400},
        )
        ctx = _ctx(score_db, members=members, now=now)
        assert await pick_variant("newbie@x.com", "dunning", 0, ctx=ctx) == BRANCH_NEW

    async def test_unknown_first_seen_falls_back(self, score_db):
        ctx = _ctx(score_db, members=_StubMembers(), now=1_700_000_000)
        assert await pick_variant("x@x.com", "dunning", 0, ctx=ctx) == BRANCH_DEFAULT


# ---------------------------------------------------------------------------
# reengagement: WAS_HOT / WAS_COLD on prior_tier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestReengagementRouting:
    async def test_previously_hot_routes_was_hot(self, score_db, monkeypatch):
        now = 1_700_000_000
        await _seed_score(
            score_db, email="prev@x.com", tier=TIER_HOT,
            score=12, when=now - 35 * 86400,
        )
        await _seed_score(
            score_db, email="prev@x.com", tier=TIER_COLD,
            score=1, when=now - 1 * 86400,
        )
        import src.email_bot.engagement_scoring as m
        monkeypatch.setattr(m.time, "time", lambda: now)
        branch = await pick_variant(
            "prev@x.com", "reengagement", 1, ctx=_ctx(score_db),
        )
        assert branch == BRANCH_WAS_HOT

    async def test_previously_cold_routes_was_cold(self, score_db, monkeypatch):
        now = 1_700_000_000
        await _seed_score(
            score_db, email="lurker@x.com", tier=TIER_COLD,
            score=1, when=now - 45 * 86400,
        )
        await _seed_score(
            score_db, email="lurker@x.com", tier=TIER_COLD,
            score=0, when=now - 1 * 86400,
        )
        import src.email_bot.engagement_scoring as m
        monkeypatch.setattr(m.time, "time", lambda: now)
        branch = await pick_variant(
            "lurker@x.com", "reengagement", 1, ctx=_ctx(score_db),
        )
        assert branch == BRANCH_WAS_COLD

    async def test_no_history_falls_back(self, score_db):
        branch = await pick_variant(
            "ghost@x.com", "reengagement", 1, ctx=_ctx(score_db),
        )
        assert branch == BRANCH_DEFAULT


# ---------------------------------------------------------------------------
# inactive_day10: WAS_ACTIVE / NEVER_ACTIVE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestInactiveDay10Routing:
    async def test_was_warm_routes_was_active(self, score_db, monkeypatch):
        now = 1_700_000_000
        await _seed_score(
            score_db, email="x@x.com", tier=TIER_WARM,
            score=5, when=now - 20 * 86400,
        )
        import src.email_bot.engagement_scoring as m
        monkeypatch.setattr(m.time, "time", lambda: now)
        assert await pick_variant(
            "x@x.com", "inactive_day10", 0, ctx=_ctx(score_db),
        ) == BRANCH_WAS_ACTIVE

    async def test_was_cold_routes_never_active(self, score_db, monkeypatch):
        now = 1_700_000_000
        await _seed_score(
            score_db, email="x@x.com", tier=TIER_COLD,
            score=0, when=now - 20 * 86400,
        )
        import src.email_bot.engagement_scoring as m
        monkeypatch.setattr(m.time, "time", lambda: now)
        assert await pick_variant(
            "x@x.com", "inactive_day10", 0, ctx=_ctx(score_db),
        ) == BRANCH_NEVER_ACTIVE


# ---------------------------------------------------------------------------
# paid_at_risk: DROPPED / NEVER_ENGAGED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPaidAtRiskRouting:
    async def test_previously_engaged_now_cold_routes_dropped(
        self, score_db, monkeypatch,
    ):
        now = 1_700_000_000
        # 70 days ago they were HOT, today they're cold.
        await _seed_score(
            score_db, email="x@x.com", tier=TIER_HOT,
            score=12, when=now - 70 * 86400,
        )
        await _seed_score(
            score_db, email="x@x.com", tier=TIER_COLD,
            score=0, when=now - 1 * 86400,
        )
        import src.email_bot.engagement_scoring as m
        monkeypatch.setattr(m.time, "time", lambda: now)
        assert await pick_variant(
            "x@x.com", "paid_at_risk", 0, ctx=_ctx(score_db),
        ) == BRANCH_DROPPED

    async def test_never_engaged_routes_never_engaged(
        self, score_db, monkeypatch,
    ):
        now = 1_700_000_000
        # Only ever cold.
        await _seed_score(
            score_db, email="x@x.com", tier=TIER_COLD,
            score=0, when=now - 70 * 86400,
        )
        await _seed_score(
            score_db, email="x@x.com", tier=TIER_COLD,
            score=0, when=now - 1 * 86400,
        )
        import src.email_bot.engagement_scoring as m
        monkeypatch.setattr(m.time, "time", lambda: now)
        assert await pick_variant(
            "x@x.com", "paid_at_risk", 0, ctx=_ctx(score_db),
        ) == BRANCH_NEVER_ENGAGED


# ---------------------------------------------------------------------------
# save_offer: short-circuits to default (routed elsewhere)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSaveOfferRouting:
    async def test_always_default(self, score_db):
        # Even with an exit reason set, save_offer hands back default so
        # the existing save_offer_router stays authoritative.
        subs = _StubSubscribers(reasons={"u@x.com": "too_expensive"})
        ctx = _ctx(score_db, subscribers=subs)
        assert await pick_variant("u@x.com", "save_offer", 0, ctx=ctx) == BRANCH_DEFAULT


# ---------------------------------------------------------------------------
# Dispatcher safety: unknown sequence + exception isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDispatcherSafety:
    async def test_unknown_sequence_returns_default(self, score_db):
        branch = await pick_variant(
            "u@x.com", "nonexistent_sequence", 0, ctx=_ctx(score_db),
        )
        assert branch == BRANCH_DEFAULT

    async def test_routing_exception_returns_default(
        self, score_db, monkeypatch,
    ):
        # Force the bronze routing function to blow up. The dispatcher
        # should swallow the exception and hand back default rather than
        # propagating.
        import src.email_bot.branch_evaluator as m

        async def _boom(*a, **k):
            raise RuntimeError("simulated DB failure")

        monkeypatch.setitem(m._DISPATCH, "bronze", _boom)
        branch = await pick_variant(
            "u@x.com", "bronze", 3, ctx=_ctx(score_db),
        )
        assert branch == BRANCH_DEFAULT
