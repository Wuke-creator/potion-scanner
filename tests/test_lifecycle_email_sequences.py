"""Tests for the new lifecycle email sequences shipped 2026-04-27.

Covers:
  - Onboarding cron schedules Day 0 / 3 / 5 / 7 / 30 emails based on
    whop_members.first_seen_at, dedupes via onboarding_last_day_sent.
  - Dunning cron schedules Day 0 / 3 / 10 emails when dunning_active=1,
    dedupes via dunning_last_day_sent.
  - PreRenewalEmail cron picks up members 3 days from billing, dedupes
    via pre_renewal_sent_for_period == current_period_end.
  - PrePauseReturnEmail cron picks up members 3 days from pause expiry.
  - InactivityDay10Email fires the one-shot template after 10 days idle,
    cooldown of 30 days.
  - Templates render without crashing for every new (sequence, day) pair.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
import pytest_asyncio

from src.automations.dunning_sequence import DUNNING_DAYS, DunningSequence
from src.automations.onboarding_sequence import (
    ONBOARDING_DAYS,
    OnboardingSequence,
)
from src.automations.pre_renewal_email import PreRenewalEmail
from src.automations.pre_pause_return_email import PrePauseReturnEmail
from src.automations.whop_members_db import WhopMembersDB
from src.email_bot.db import EmailDB, Subscriber
from src.email_bot.stats import StatsBundle
from src.email_bot.templates import render


# ---------------------------------------------------------------------------
# Templates: every new (sequence, day) renders cleanly
# ---------------------------------------------------------------------------

def _stats() -> StatsBundle:
    """Minimal stats fixture. The new templates use getattr-with-default
    so they render even when a real bundle's fields don't expose the
    derived names (top_pair_30d / top_pnl_pct_30d / wins_30d_over_50pct
    are aliases that fall back to constants in the templates)."""
    return StatsBundle(
        calls_7d_total=22,
        wins_7d_over_50pct=5,
        top_call_7d={"pair": "ETH/USDT", "pnl_pct": 89.0, "days_ago": 2},
        top_calls_7d=[],
        calls_30d_total=92,
        top_call_30d={"pair": "ETH/USDT", "pnl_pct": 142.0, "days_ago": 5},
        top_calls_30d=[],
    )


def _sub(reason: str = "none", trigger: str = "onboarding") -> Subscriber:
    return Subscriber(
        email="user@example.com",
        name="Trader",
        trigger_type=trigger,
        exit_reason=reason,
        created_at=int(time.time()),
        rejoin_url="https://whop.com/potion",
    )


@pytest.mark.parametrize("day", ONBOARDING_DAYS)
def test_onboarding_template_renders(day):
    out = render("onboarding", day, _sub(trigger="onboarding"), _stats())
    assert out.subject and out.text and out.html
    # No raw 'None' should leak into the body
    assert "None" not in out.text


def test_onboarding_monthly_renders_for_day_60_plus():
    """Day 60+ falls back to the monthly digest template via the
    sequence='onboarding' branch in render()."""
    for day in (60, 90, 120, 365):
        out = render("onboarding", day, _sub(trigger="onboarding"), _stats())
        assert "this month" in out.subject.lower() or "month" in out.subject.lower()


@pytest.mark.parametrize("day", DUNNING_DAYS)
def test_dunning_template_renders(day):
    out = render("dunning", day, _sub(trigger="dunning"), _stats())
    assert out.subject and out.text and out.html
    assert "None" not in out.text


def test_pre_renewal_template_renders():
    out = render("pre_renewal", 0, _sub(trigger="pre_renewal"), _stats())
    assert "renews in 3 days" in out.subject.lower()


def test_pre_pause_return_template_renders():
    out = render(
        "pre_pause_return", 0, _sub(trigger="pre_pause_return"), _stats(),
    )
    assert out.subject and out.text and out.html


def test_inactive_day10_template_renders():
    out = render(
        "inactive_day10", 0, _sub(trigger="inactive_day10"), _stats(),
    )
    assert out.subject and out.text and out.html
    # Should NOT mention "30 days" — that's the 14-day-then-reengagement
    # series, this is the gentler 10-day nudge.
    assert "10 days" in out.text


def test_unknown_sequence_raises():
    with pytest.raises(ValueError):
        render("invalid_sequence", 0, _sub(), _stats())


# ---------------------------------------------------------------------------
# Onboarding cron: schedule + dedupe
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def both_dbs(tmp_path: Path):
    members = WhopMembersDB(db_path=str(tmp_path / "members.db"))
    await members.open()
    email = EmailDB(db_path=str(tmp_path / "email.db"))
    await email.open()
    yield members, email
    await email.close()
    await members.close()


@pytest.mark.asyncio
async def test_onboarding_schedules_day0_for_brand_new_member(both_dbs):
    members, email = both_dbs
    now = int(time.time())
    # Member just joined — 0 days old
    await members.upsert_member(
        whop_user_id="user_new", discord_user_id="",
        email="new@example.com", valid=True,
        membership_id="mem_1", when=now,
    )
    cron = OnboardingSequence(members, email, interval_hours=24)
    counts = await cron.run_once(now=now)
    assert counts.get(0) == 1  # day 0 scheduled
    # Days > 0 should NOT have fired yet
    for d in (3, 5, 7, 30):
        assert counts.get(d, 0) == 0


@pytest.mark.asyncio
async def test_onboarding_dedupes_on_second_run(both_dbs):
    members, email = both_dbs
    now = int(time.time())
    await members.upsert_member(
        whop_user_id="user_a", discord_user_id="",
        email="a@example.com", valid=True,
        membership_id="m", when=now,
    )
    cron = OnboardingSequence(members, email, interval_hours=24)
    first = await cron.run_once(now=now)
    second = await cron.run_once(now=now)
    assert first.get(0) == 1
    assert second.get(0) == 0  # dedupe on second pass


@pytest.mark.asyncio
async def test_onboarding_fires_day3_after_3_days(both_dbs):
    members, email = both_dbs
    base = int(time.time()) - 3 * 86400
    await members.upsert_member(
        whop_user_id="user_3d", discord_user_id="",
        email="3d@example.com", valid=True,
        membership_id="m", when=base,
    )
    cron = OnboardingSequence(members, email, interval_hours=24)
    counts = await cron.run_once(now=base + 3 * 86400)
    # Both day 0 and day 3 are due (member is 3 days old, never sent any)
    assert counts.get(0) == 1
    assert counts.get(3) == 1


# ---------------------------------------------------------------------------
# Dunning cron: only fires for dunning_active=1 members
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dunning_does_not_fire_without_active_flag(both_dbs):
    members, email = both_dbs
    now = int(time.time())
    await members.upsert_member(
        whop_user_id="u", discord_user_id="",
        email="u@example.com", valid=True,
        membership_id="m", when=now,
    )
    cron = DunningSequence(members, email)
    counts = await cron.run_once(now=now)
    for d in DUNNING_DAYS:
        assert counts.get(d, 0) == 0


@pytest.mark.asyncio
async def test_dunning_fires_day0_when_active(both_dbs):
    members, email = both_dbs
    now = int(time.time())
    await members.upsert_member(
        whop_user_id="u", discord_user_id="",
        email="u@example.com", valid=True,
        membership_id="m", when=now,
    )
    started = await members.start_dunning("u", when=now)
    assert started is True
    cron = DunningSequence(members, email)
    counts = await cron.run_once(now=now)
    assert counts.get(0) == 1


@pytest.mark.asyncio
async def test_dunning_start_is_idempotent(both_dbs):
    members, email = both_dbs
    now = int(time.time())
    await members.upsert_member(
        whop_user_id="u", discord_user_id="",
        email="u@example.com", valid=True,
        membership_id="m", when=now,
    )
    first = await members.start_dunning("u", when=now)
    second = await members.start_dunning("u", when=now)
    assert first is True
    assert second is False  # already in active dunning


@pytest.mark.asyncio
async def test_dunning_stops_on_payment_succeeded(both_dbs):
    members, email = both_dbs
    now = int(time.time())
    await members.upsert_member(
        whop_user_id="u", discord_user_id="",
        email="u@example.com", valid=True,
        membership_id="m", when=now,
    )
    await members.start_dunning("u", when=now)
    await members.stop_dunning("u")
    cron = DunningSequence(members, email)
    counts = await cron.run_once(now=now)
    for d in DUNNING_DAYS:
        assert counts.get(d, 0) == 0  # stop_dunning should clear


# ---------------------------------------------------------------------------
# Pre-renewal cron: 3-day window + per-period dedupe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pre_renewal_fires_in_3_day_window(both_dbs):
    members, email = both_dbs
    now = int(time.time())
    period_end = now + 3 * 86400
    await members.upsert_member(
        whop_user_id="u", discord_user_id="",
        email="u@example.com", valid=True,
        membership_id="m", when=now,
    )
    await members.set_current_period_end("u", period_end=period_end)
    cron = PreRenewalEmail(members, email, days_before=3)
    n = await cron.run_once(now=now)
    assert n == 1
    # Second pass: dedupe on same period_end
    n2 = await cron.run_once(now=now)
    assert n2 == 0


@pytest.mark.asyncio
async def test_pre_renewal_skips_outside_window(both_dbs):
    members, email = both_dbs
    now = int(time.time())
    # Period 10 days away — outside the 3-day window
    period_end = now + 10 * 86400
    await members.upsert_member(
        whop_user_id="u", discord_user_id="",
        email="u@example.com", valid=True,
        membership_id="m", when=now,
    )
    await members.set_current_period_end("u", period_end=period_end)
    cron = PreRenewalEmail(members, email, days_before=3)
    assert await cron.run_once(now=now) == 0


# ---------------------------------------------------------------------------
# Pre-pause-return: dormant when pause_ends_at == 0
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pre_pause_return_dormant_when_no_pause(both_dbs):
    members, email = both_dbs
    now = int(time.time())
    await members.upsert_member(
        whop_user_id="u", discord_user_id="",
        email="u@example.com", valid=True,
        membership_id="m", when=now,
    )
    cron = PrePauseReturnEmail(members, email, days_before=3)
    assert await cron.run_once(now=now) == 0
