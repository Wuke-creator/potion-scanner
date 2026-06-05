"""Tests for the sync-driven Bronze -> Elite upsell (Option 2).

The safety-critical test is `test_pre_golive_member_not_enrolled`: it
proves the existing free-signup backlog can never be blasted, which is
the whole reason the go-live cutoff exists.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from src.automations.bronze_upsell import BronzeUpsell
from src.email_bot.db import EmailDB, Subscriber

FREE = "prod_FREE"
PAID = "prod_PAID"
GO_LIVE = 1_000_000  # arbitrary epoch cutoff for tests


@dataclass
class FakeMember:
    email: str
    valid: bool
    product: str
    created_at: int = 0
    discord_user_id: str = ""


@pytest_asyncio.fixture
async def email_db(tmp_path: Path):
    db = EmailDB(db_path=str(tmp_path / "email.db"))
    await db.open()
    yield db
    await db.close()


def _bronze(db, free=FREE, go_live=GO_LIVE):
    return BronzeUpsell(
        email_db=db, free_product_id=free, go_live_at=go_live,
        rejoin_url="https://whop.com/potion",
    )


# ---- dormancy guards ------------------------------------------------------

@pytest.mark.asyncio
async def test_dormant_without_product_id(email_db):
    b = _bronze(email_db, free="")
    assert b.is_enabled is False
    b.observe(FakeMember("a@x.com", True, FREE, GO_LIVE + 1))
    out = await b.finalize()
    assert out["status"] == "disabled"


@pytest.mark.asyncio
async def test_dormant_when_go_live_zero(email_db):
    # The backlog guard: go_live_at=0 means enrol nobody, ever.
    b = _bronze(email_db, go_live=0)
    assert b.is_enabled is False
    b.observe(FakeMember("a@x.com", True, FREE, 2_000_000))
    out = await b.finalize()
    assert out["status"] == "disabled"
    assert await email_db.get_subscriber("a@x.com") is None


# ---- enrolment ------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_free_member_enrolled(email_db):
    b = _bronze(email_db)
    b.observe(FakeMember("new@x.com", True, FREE, GO_LIVE + 100))
    out = await b.finalize()
    assert out["enrolled"] == 1
    sub = await email_db.get_subscriber("new@x.com")
    assert sub is not None and sub.trigger_type == "bronze"
    # 5 bronze sends scheduled (days 0/1/3/5/7) since the 2026-05-19
    # behaviour-tree extension. schedule_sequence uses real time.time();
    # query a window 8 days out so the D7 send is comfortably included.
    horizon = int(time.time()) + 8 * 86400
    bronze_sends = [
        s for s in await email_db.due_sends(now=horizon)
        if s.sequence == "bronze"
    ]
    assert len(bronze_sends) == 5


@pytest.mark.asyncio
async def test_pre_golive_member_not_enrolled(email_db):
    # SAFETY: a free member whose membership predates go-live is the
    # existing backlog. Must NEVER be enrolled.
    b = _bronze(email_db)
    b.observe(FakeMember("old@x.com", True, FREE, GO_LIVE - 1))
    out = await b.finalize()
    assert out["enrolled"] == 0
    assert out["skipped_pre_golive"] == 1
    assert await email_db.get_subscriber("old@x.com") is None


@pytest.mark.asyncio
async def test_paid_member_not_enrolled(email_db):
    b = _bronze(email_db)
    # Holds a valid paid plan (even though also free) -> skip, never upsell.
    b.observe(FakeMember("paid@x.com", True, FREE, GO_LIVE + 5))
    b.observe(FakeMember("paid@x.com", True, PAID, GO_LIVE + 5))
    out = await b.finalize()
    assert out["enrolled"] == 0
    assert out["skipped_paid"] == 1
    assert await email_db.get_subscriber("paid@x.com") is None


@pytest.mark.asyncio
async def test_invalid_membership_ignored(email_db):
    b = _bronze(email_db)
    # An invalid free membership must not count as "has free".
    b.observe(FakeMember("inv@x.com", False, FREE, GO_LIVE + 9))
    out = await b.finalize()
    assert out["enrolled"] == 0
    assert await email_db.get_subscriber("inv@x.com") is None


@pytest.mark.asyncio
async def test_already_enrolled_not_duplicated(email_db):
    b = _bronze(email_db)
    b.observe(FakeMember("dupe@x.com", True, FREE, GO_LIVE + 1))
    first = await b.finalize()
    assert first["enrolled"] == 1
    # Second sync cycle sees the same free member again.
    b2 = _bronze(email_db)
    b2.observe(FakeMember("dupe@x.com", True, FREE, GO_LIVE + 1))
    second = await b2.finalize()
    assert second["enrolled"] == 0
    assert second["already_enrolled"] == 1


# ---- upgrade-stop ---------------------------------------------------------

@pytest.mark.asyncio
async def test_upgrade_cancels_pending_bronze(email_db):
    # Enrol, then they convert to paid -> remaining bronze sends cancelled.
    horizon = int(time.time()) + 10 * 86400
    b = _bronze(email_db)
    b.observe(FakeMember("conv@x.com", True, FREE, GO_LIVE + 2))
    await b.finalize()
    pending_before = [
        s for s in await email_db.due_sends(now=horizon)
        if s.sequence == "bronze"
    ]
    # 5 since the 2026-05-19 D0+D7 extension.
    assert len(pending_before) == 5

    b2 = _bronze(email_db)
    b2.observe(FakeMember("conv@x.com", True, FREE, GO_LIVE + 2))
    b2.observe(FakeMember("conv@x.com", True, PAID, GO_LIVE + 9))
    out = await b2.finalize()
    assert out["upgrade_stopped"] == 1
    pending_after = [
        s for s in await email_db.due_sends(now=horizon)
        if s.sequence == "bronze"
    ]
    assert pending_after == []


@pytest.mark.asyncio
async def test_cancel_pending_db_method(email_db):
    await email_db.upsert_subscriber(Subscriber(
        email="c@x.com", name="C", trigger_type="bronze",
        exit_reason="none", rejoin_url="u", created_at=int(time.time()),
    ))
    await email_db.schedule_sequence(email="c@x.com", sequence="bronze")
    n = await email_db.cancel_pending("c@x.com", "bronze")
    # 5 since the 2026-05-19 D0+D7 extension.
    assert n == 5
    # Idempotent: nothing left pending.
    assert await email_db.cancel_pending("c@x.com", "bronze") == 0


# ---- _parse_member tier capture ------------------------------------------

def test_parse_member_captures_product_and_created_at():
    from src.whop_api import _parse_member
    m = _parse_member({
        "id": "mem_1", "user": "user_1", "email": "P@X.com",
        "valid": True, "status": "completed",
        "product": "prod_LVAuYCd2uhi7y", "access_pass": "prod_LVAuYCd2uhi7y",
        "created_at": 1781567935,
    })
    assert m is not None
    assert m.product == "prod_LVAuYCd2uhi7y"
    assert m.created_at == 1781567935
    assert m.email == "p@x.com"
