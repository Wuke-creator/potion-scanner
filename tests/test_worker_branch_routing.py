"""Integration tests for the branch_evaluator -> EmailWorker wiring.

Covers the four properties the wire-up has to preserve:

  1. With a real EvalContext set, the worker consults branch_evaluator
     and passes the resulting label to render() AND mark_sent().
  2. With no EvalContext (the dormant production state until main.py
     wires one), the worker behaves byte-for-byte as before: no
     branch routing, no branch stamped on the row.
  3. When branch_evaluator returns "default" (no signal yet, missing
     data, unknown sequence), the worker still falls through to the
     day-keyed renderer and stores a NULL branch column.
  4. When branch_evaluator itself raises, the worker swallows the
     exception, logs it, and falls through to the default renderer.
     No send may ever fail because of a branch lookup.

We exercise _deliver_one directly. The full delivery loop is covered
by the existing test_suppression_gate and test_bronze_sequence files
already; here we focus on the render() path and the branch column
persistence.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src.email_bot.branch_evaluator import (
    BRANCH_DEFAULT,
    BRANCH_HIGH,
    BRANCH_HOT,
    BRANCH_LOW,
    EvalContext,
)
from src.email_bot.db import EmailDB, ScheduledSend, Subscriber
from src.email_bot.engagement_scoring import (
    EngagementScore,
    EngagementScoreDB,
    HOT_THRESHOLD,
    TIER_COLD,
    TIER_HOT,
)
from src.email_bot.sender import SendResult
from src.email_bot.worker import EmailWorker


# ---------------------------------------------------------------------------
# Stubs and fixtures
# ---------------------------------------------------------------------------


class _OkSender:
    """Stand-in for ResendClient that always succeeds and records the call.

    Accepts **kwargs so it stays forward-compatible with the worker
    plumbing through extra fields like unsubscribe_url without test
    breakage.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send(self, **kwargs) -> SendResult:
        self.calls.append(kwargs)
        return SendResult(ok=True, resend_id="re_test_id", error=None)


def _stats() -> SimpleNamespace:
    """Minimal stats bundle covering every attribute templates touch."""
    return SimpleNamespace(
        calls_7d_total=22, wins_7d_over_50pct=5,
        top_call_7d={"pair": "ETH/USDT", "pnl_pct": 89.0, "days_ago": 2},
        top_calls_7d=[{"pair": "ETH/USDT", "pnl_pct": 89.0, "days_ago": 2}],
        calls_30d_total=92, wins_30d_over_50pct=18,
        top_call_30d={"pair": "SOL/USDT", "pnl_pct": 142.0, "days_ago": 18},
        top_calls_30d=[
            {"pair": "SOL/USDT", "pnl_pct": 142.0, "days_ago": 18},
            {"pair": "BONK/USDT", "pnl_pct": 115.0, "days_ago": 25},
        ],
        top_pair_7d="ETH/USDT", top_pct_7d=89,
        top_pair_30d="SOL/USDT", top_pnl_pct_30d=142,
        alerts_7d_flagged=12,
    )


@pytest_asyncio.fixture
async def email_db(tmp_path: Path):
    db = EmailDB(db_path=str(tmp_path / "email.db"))
    await db.open()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def score_db(tmp_path: Path):
    db = EngagementScoreDB(db_path=str(tmp_path / "events.db"))
    await db.open()
    yield db
    await db.close()


async def _seed_subscriber(email_db: EmailDB, email: str) -> None:
    await email_db.upsert_subscriber(Subscriber(
        email=email, name="Test",
        trigger_type="cancellation", exit_reason="other",
        rejoin_url="https://whop.com/potion", created_at=int(time.time()),
    ))


async def _seed_score(
    score_db: EngagementScoreDB,
    email: str,
    *,
    score: int,
    tier: str,
) -> None:
    await score_db.upsert(EngagementScore(
        email=email.lower(), score=float(score), tier=tier,
        opens_30d=float(score), clicks_30d=0.0,
        last_open_at=int(time.time()),
        last_click_at=None,
        days_since_last_event=1,
        updated_at=int(time.time()),
    ))


def _ctx(
    score_db: EngagementScoreDB,
    *,
    now: int | None = None,
) -> EvalContext:
    return EvalContext(score_db=score_db, members=None, subscribers=None, now=now)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBronzeBranching:
    """Bronze routes on engagement tier at days 3 and 5."""

    async def test_hot_user_at_day3_gets_default_no_warm_variant_exists(
        self, email_db, score_db,
    ):
        # Day 3 only has WARM + COLD variants; hot falls through to default.
        await _seed_subscriber(email_db, "hot@x.com")
        await _seed_score(score_db, "hot@x.com", score=HOT_THRESHOLD + 5, tier=TIER_HOT)
        send_id = await email_db.schedule_one(
            email="hot@x.com", sequence="bronze", day=3, due_at=int(time.time()),
        )
        sender = _OkSender()
        worker = EmailWorker(
            db=email_db, sender=sender, analytics_db_path="x",
            branch_evaluator_ctx=_ctx(score_db),
        )
        send = ScheduledSend(
            id=send_id, email="hot@x.com", sequence="bronze", day=3,
            due_at=int(time.time()), sent_at=None, status="pending",
            error=None, resend_id=None,
        )
        await worker._deliver_one(send, stats=_stats())
        # Render succeeded -> sender was called.
        assert len(sender.calls) == 1
        # mark_sent stamped the branch label even though it fell through to
        # the default renderer (the label captures what the evaluator
        # picked, not which renderer fired).
        async with email_db._conn.execute(
            "SELECT branch FROM scheduled_sends WHERE id = ?", (send_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == BRANCH_HOT

    async def test_no_score_falls_back_to_default(self, email_db, score_db):
        # No row in engagement_score -> branch_evaluator returns DEFAULT.
        await _seed_subscriber(email_db, "unknown@x.com")
        send_id = await email_db.schedule_one(
            email="unknown@x.com", sequence="bronze", day=3,
            due_at=int(time.time()),
        )
        sender = _OkSender()
        worker = EmailWorker(
            db=email_db, sender=sender, analytics_db_path="x",
            branch_evaluator_ctx=_ctx(score_db),
        )
        send = ScheduledSend(
            id=send_id, email="unknown@x.com", sequence="bronze", day=3,
            due_at=int(time.time()), sent_at=None, status="pending",
            error=None, resend_id=None,
        )
        await worker._deliver_one(send, stats=_stats())
        assert len(sender.calls) == 1
        async with email_db._conn.execute(
            "SELECT branch FROM scheduled_sends WHERE id = ?", (send_id,),
        ) as cur:
            row = await cur.fetchone()
        # DEFAULT means we don't stamp a branch -- the renderer used the
        # default path and there's no variant signal worth storing.
        assert row[0] is None


@pytest.mark.asyncio
class TestPreRenewalBranching:
    """pre_renewal routes on HIGH vs LOW engagement."""

    async def test_high_score_routes_to_high_variant(self, email_db, score_db):
        await _seed_subscriber(email_db, "high@x.com")
        await _seed_score(
            score_db, "high@x.com",
            score=HOT_THRESHOLD + 1, tier=TIER_HOT,
        )
        send_id = await email_db.schedule_one(
            email="high@x.com", sequence="pre_renewal", day=0,
            due_at=int(time.time()),
        )
        sender = _OkSender()
        worker = EmailWorker(
            db=email_db, sender=sender, analytics_db_path="x",
            branch_evaluator_ctx=_ctx(score_db),
        )
        send = ScheduledSend(
            id=send_id, email="high@x.com", sequence="pre_renewal", day=0,
            due_at=int(time.time()), sent_at=None, status="pending",
            error=None, resend_id=None,
        )
        await worker._deliver_one(send, stats=_stats())
        # The HIGH variant subject starts with "Your Elite renews".
        assert "renews" in sender.calls[0]["subject"].lower()
        async with email_db._conn.execute(
            "SELECT branch FROM scheduled_sends WHERE id = ?", (send_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == BRANCH_HIGH

    async def test_low_score_routes_to_low_variant(self, email_db, score_db):
        await _seed_subscriber(email_db, "low@x.com")
        await _seed_score(
            score_db, "low@x.com",
            score=1, tier=TIER_COLD,
        )
        send_id = await email_db.schedule_one(
            email="low@x.com", sequence="pre_renewal", day=0,
            due_at=int(time.time()),
        )
        sender = _OkSender()
        worker = EmailWorker(
            db=email_db, sender=sender, analytics_db_path="x",
            branch_evaluator_ctx=_ctx(score_db),
        )
        send = ScheduledSend(
            id=send_id, email="low@x.com", sequence="pre_renewal", day=0,
            due_at=int(time.time()), sent_at=None, status="pending",
            error=None, resend_id=None,
        )
        await worker._deliver_one(send, stats=_stats())
        async with email_db._conn.execute(
            "SELECT branch FROM scheduled_sends WHERE id = ?", (send_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == BRANCH_LOW


@pytest.mark.asyncio
class TestFallbackBehaviour:
    """Defense-in-depth properties: the evaluator can never block a send."""

    async def test_unknown_sequence_falls_back(self, email_db, score_db):
        # The evaluator returns DEFAULT for unknown sequences. The
        # worker should still render via the existing render() path.
        await _seed_subscriber(email_db, "x@x.com")
        # Use "onboarding" day=0 -- a valid render path but with no
        # branch variant defined for it.
        send_id = await email_db.schedule_one(
            email="x@x.com", sequence="onboarding", day=0,
            due_at=int(time.time()),
        )
        sender = _OkSender()
        worker = EmailWorker(
            db=email_db, sender=sender, analytics_db_path="x",
            branch_evaluator_ctx=_ctx(score_db),
        )
        send = ScheduledSend(
            id=send_id, email="x@x.com", sequence="onboarding", day=0,
            due_at=int(time.time()), sent_at=None, status="pending",
            error=None, resend_id=None,
        )
        await worker._deliver_one(send, stats=_stats())
        assert len(sender.calls) == 1

    async def test_branch_evaluator_exception_falls_back_to_default(
        self, email_db, score_db, monkeypatch,
    ):
        """If pick_variant itself raises (not its routing fn -- that's
        caught inside the dispatcher), the worker swallows and falls
        through. We simulate this by monkeypatching pick_variant."""
        await _seed_subscriber(email_db, "boom@x.com")
        send_id = await email_db.schedule_one(
            email="boom@x.com", sequence="bronze", day=1,
            due_at=int(time.time()),
        )

        async def _broken(*args, **kwargs):
            raise RuntimeError("simulated evaluator outage")

        monkeypatch.setattr(
            "src.email_bot.worker.pick_variant", _broken,
        )
        sender = _OkSender()
        worker = EmailWorker(
            db=email_db, sender=sender, analytics_db_path="x",
            branch_evaluator_ctx=_ctx(score_db),
        )
        send = ScheduledSend(
            id=send_id, email="boom@x.com", sequence="bronze", day=1,
            due_at=int(time.time()), sent_at=None, status="pending",
            error=None, resend_id=None,
        )
        await worker._deliver_one(send, stats=_stats())
        # The send still went out via the default renderer.
        assert len(sender.calls) == 1
        # The branch column is NULL because the evaluator failed.
        async with email_db._conn.execute(
            "SELECT branch, status FROM scheduled_sends WHERE id = ?",
            (send_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row[0] is None
        assert row[1] == "sent"

    async def test_no_ctx_means_no_branching(self, email_db, score_db):
        """Without a branch_evaluator_ctx the worker behaves byte-for-byte
        as before: it goes straight to render() with no branch label and
        leaves the branch column NULL on mark_sent."""
        await _seed_subscriber(email_db, "noctx@x.com")
        # Even with a HOT score in the DB, no ctx means no routing.
        await _seed_score(
            score_db, "noctx@x.com",
            score=HOT_THRESHOLD + 5, tier=TIER_HOT,
        )
        send_id = await email_db.schedule_one(
            email="noctx@x.com", sequence="bronze", day=3,
            due_at=int(time.time()),
        )
        sender = _OkSender()
        worker = EmailWorker(
            db=email_db, sender=sender, analytics_db_path="x",
            # Critical: no branch_evaluator_ctx
        )
        send = ScheduledSend(
            id=send_id, email="noctx@x.com", sequence="bronze", day=3,
            due_at=int(time.time()), sent_at=None, status="pending",
            error=None, resend_id=None,
        )
        await worker._deliver_one(send, stats=_stats())
        assert len(sender.calls) == 1
        async with email_db._conn.execute(
            "SELECT branch FROM scheduled_sends WHERE id = ?", (send_id,),
        ) as cur:
            row = await cur.fetchone()
        # No ctx -> the branch column stays NULL even though a HOT score
        # would have been picked if we had asked.
        assert row[0] is None


@pytest.mark.asyncio
class TestMarkSentStampsBranch:
    """The branch column lands on the row only on successful send."""

    async def test_failed_send_does_not_stamp_branch(self, email_db, score_db):
        await _seed_subscriber(email_db, "fail@x.com")
        await _seed_score(score_db, "fail@x.com", score=20, tier=TIER_HOT)
        send_id = await email_db.schedule_one(
            email="fail@x.com", sequence="bronze", day=1,
            due_at=int(time.time()),
        )

        class _BadSender:
            async def send(self, **kwargs) -> SendResult:  # noqa: U100
                return SendResult(ok=False, resend_id=None, error="boom")

        worker = EmailWorker(
            db=email_db, sender=_BadSender(), analytics_db_path="x",
            branch_evaluator_ctx=_ctx(score_db),
        )
        send = ScheduledSend(
            id=send_id, email="fail@x.com", sequence="bronze", day=1,
            due_at=int(time.time()), sent_at=None, status="pending",
            error=None, resend_id=None,
        )
        await worker._deliver_one(send, stats=_stats())
        async with email_db._conn.execute(
            "SELECT status, branch FROM scheduled_sends WHERE id = ?",
            (send_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == "failed"
        # mark_failed does not touch the branch column.
        assert row[1] is None
