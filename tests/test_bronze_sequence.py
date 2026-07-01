"""Tests for the Bronze -> Elite upsell wiring.

Covers the three layers added to make the bronze templates actually
deliver:
  - db: 'bronze' registered + (1,3,5) schedule
  - worker: day-5 mints a single-use promo + overrides rejoin_url;
    days 1/3 and mint-failure leave the plain link untouched
  - webhook: membership.activated enrols only the configured Bronze
    pass, dedupes, and stays dormant when the pass id is unset
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src.email_bot.db import EmailDB, Subscriber
from src.email_bot.webhook import EmailWebhookHandlers
from src.email_bot.worker import EmailWorker


@pytest_asyncio.fixture
async def email_db(tmp_path: Path):
    db = EmailDB(db_path=str(tmp_path / "email.db"))
    await db.open()
    yield db
    await db.close()


# ---- db: sequence registration -------------------------------------------

@pytest.mark.asyncio
async def test_bronze_registered_and_scheduled(email_db: EmailDB):
    assert "bronze" in email_db.KNOWN_SEQUENCES
    # Bronze offsets extended 2026-05-19 from (1,3,5) to (0,1,3,5,7) as
    # part of the behaviour-tree work. D0 welcomes + drives first Discord
    # visit; D7 is last-call urgency on the BRONZE30 code minted at D5.
    assert email_db._default_offsets("bronze") == (0, 1, 3, 5, 7)
    await email_db.upsert_subscriber(Subscriber(
        email="b@x.com", name="B", trigger_type="bronze",
        exit_reason="none", rejoin_url="https://whop.com/potion",
        created_at=int(time.time()),
    ))
    ids = await email_db.schedule_sequence(email="b@x.com", sequence="bronze")
    # 5 since the 2026-05-19 D0+D7 extension.
    assert len(ids) == 5


@pytest.mark.asyncio
async def test_bronze_not_cancel_on_reschedule(email_db: EmailDB):
    # bronze is an independent lifecycle; enrolling must not wipe other
    # pending sequences and isn't wiped by winback/reengagement.
    assert "bronze" not in email_db._CANCEL_ON_RESCHEDULE


# ---- worker: day-5 promo minting -----------------------------------------

def _worker(promo_key="k", company="c"):
    return EmailWorker(
        db=MagicMock(), sender=MagicMock(), analytics_db_path="x",
        whop_promo_api_key=promo_key, whop_company_id=company,
        bronze_promo_ttl_days=14,
    )


def _sub(url="https://whop.com/potion"):
    return Subscriber(
        email="u@x.com", name="U", trigger_type="bronze",
        exit_reason="none", rejoin_url=url, created_at=0,
    )


@pytest.mark.asyncio
async def test_day5_returns_plain_url_no_promo(monkeypatch):
    # Promo codes were removed from Bronze emails: day 5 now returns the
    # subscriber unchanged on the plain rejoin URL and never mints a code.
    w = _worker()
    mock_mint = AsyncMock(return_value="BRONZE30-AB12CD")
    monkeypatch.setattr(
        "src.email_bot.worker.create_one_time_promo", mock_mint,
    )
    sub = _sub()
    out = await w._bronze_day5_subscriber(sub)
    assert out is sub
    assert out.rejoin_url == "https://whop.com/potion"
    assert "promo=" not in out.rejoin_url
    mock_mint.assert_not_awaited()  # no promo minting anymore


@pytest.mark.asyncio
async def test_day5_mint_failure_falls_back_to_plain(monkeypatch):
    w = _worker()
    monkeypatch.setattr(
        "src.email_bot.worker.create_one_time_promo",
        AsyncMock(return_value=None),
    )
    sub = _sub()
    out = await w._bronze_day5_subscriber(sub)
    assert out is sub  # unchanged, plain link, email still sends


@pytest.mark.asyncio
async def test_day5_no_promo_keys_falls_back(monkeypatch):
    w = _worker(promo_key="", company="")
    called = AsyncMock()
    monkeypatch.setattr(
        "src.email_bot.worker.create_one_time_promo", called,
    )
    sub = _sub()
    out = await w._bronze_day5_subscriber(sub)
    assert out is sub
    called.assert_not_awaited()  # never calls Whop


@pytest.mark.asyncio
async def test_day5_leaves_query_url_untouched(monkeypatch):
    # A rejoin URL that already carries a query param passes through
    # unchanged: no ?promo= / &promo= is appended.
    w = _worker()
    monkeypatch.setattr(
        "src.email_bot.worker.create_one_time_promo",
        AsyncMock(return_value="BRONZE30-XYZ999"),
    )
    sub = _sub("https://whop.com/potion?ref=x")
    out = await w._bronze_day5_subscriber(sub)
    assert out is sub
    assert out.rejoin_url == "https://whop.com/potion?ref=x"
    assert "promo=" not in out.rejoin_url


# ---- webhook: enrolment trigger ------------------------------------------

def _handlers(bronze_id="", db=None):
    return EmailWebhookHandlers(
        db=db or MagicMock(),
        whop_webhook_secret="s", admin_secret="a",
        rejoin_url_default="https://whop.com/potion",
        bronze_access_pass_id=bronze_id,
    )


def test_extract_access_pass_ids_multipath():
    h = _handlers()
    data = {
        "product_id": "prod_A",
        "membership": {"access_pass": {"id": "pass_B"}, "plan_id": "plan_C"},
    }
    ids = h._extract_access_pass_ids(data)
    assert {"prod_A", "pass_B", "plan_C"} <= ids


@pytest.mark.asyncio
async def test_enrol_dormant_when_id_unset():
    h = _handlers(bronze_id="")
    assert await h._maybe_enrol_bronze({"product_id": "anything"}) is False


@pytest.mark.asyncio
async def test_enrol_skips_when_pass_not_matched():
    h = _handlers(bronze_id="pass_BRONZE")
    out = await h._maybe_enrol_bronze({"product_id": "pass_ELITE",
                                       "email": "x@y.com"})
    assert out is False


@pytest.mark.asyncio
async def test_enrol_schedules_when_matched(email_db: EmailDB):
    h = _handlers(bronze_id="pass_BRONZE", db=email_db)
    data = {
        "access_pass": {"id": "pass_BRONZE"},
        "user": {"email": "NewBronze@X.com", "name": "Newbie"},
    }
    out = await h._maybe_enrol_bronze(data)
    assert out is True
    sub = await email_db.get_subscriber("newbronze@x.com")
    assert sub is not None and sub.trigger_type == "bronze"


@pytest.mark.asyncio
async def test_enrol_dedupes_repeated_webhooks(email_db: EmailDB):
    h = _handlers(bronze_id="pass_BRONZE", db=email_db)
    data = {"access_pass": {"id": "pass_BRONZE"},
            "email": "dupe@x.com"}
    first = await h._maybe_enrol_bronze(data)
    second = await h._maybe_enrol_bronze(data)
    assert first is True
    assert second is False  # already a bronze subscriber, not re-enrolled
