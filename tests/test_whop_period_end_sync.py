"""Whop sync -> current_period_end wiring.

The pre-renewal email cron (PreRenewalEmail) selects on
whop_members.current_period_end, which only the Whop sync can populate.
These tests pin the two halves of that wiring:

  1. _parse_member lifts renewal_period_end off the v2 membership row
     (null / garbage normalize to 0).
  2. WhopEmailSync.run_once writes it via set_current_period_end for valid
     paid rows only: free rows (0) must not wipe a paid row's date, and
     invalid rows must not write at all.
"""

import pytest
import pytest_asyncio

import src.automations.whop_email_sync as whop_email_sync_mod
from src.automations.whop_email_sync import WhopEmailSync
from src.automations.whop_members_db import WhopMembersDB
from src.whop_api import WhopMember, _parse_member


# ---- _parse_member ----------------------------------------------------


def _row(**overrides):
    base = {
        "id": "mem_1",
        "user": "user_1",
        "email": "m@example.com",
        "valid": True,
        "status": "completed",
        "discord": {"id": "111"},
        "product": "prod_paid",
        "created_at": 1700000000,
    }
    base.update(overrides)
    return base


def test_parse_member_reads_renewal_period_end():
    member = _parse_member(_row(renewal_period_end=1793000000))
    assert member.renewal_period_end == 1793000000


@pytest.mark.parametrize("raw", [None, "not-a-number", {}])
def test_parse_member_normalizes_bad_renewal_to_zero(raw):
    member = _parse_member(_row(renewal_period_end=raw))
    assert member.renewal_period_end == 0


def test_parse_member_missing_renewal_defaults_to_zero():
    member = _parse_member(_row())
    assert member.renewal_period_end == 0


# ---- sync writes the column ------------------------------------------


class _FakeVerificationDB:
    async def list_active(self):
        return []


class _FakeWhopClient:
    """Stands in for WhopAPIClient; yields a fixed membership list."""

    members: list[WhopMember] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def iter_memberships(self):
        for m in self.members:
            yield m


def _member(user_id, *, valid=True, period_end=0, membership_id="mem_x"):
    return WhopMember(
        user_id=user_id,
        discord_user_id="",
        email=f"{user_id}@example.com",
        valid=valid,
        membership_id=membership_id,
        product="prod_paid" if period_end else "prod_free",
        created_at=1700000000,
        renewal_period_end=period_end,
    )


@pytest_asyncio.fixture
async def members_db(tmp_path):
    db = WhopMembersDB(db_path=str(tmp_path / "members.db"))
    await db.open()
    yield db
    await db.close()


async def _run_sync(monkeypatch, members_db, members):
    _FakeWhopClient.members = members
    monkeypatch.setattr(whop_email_sync_mod, "WhopAPIClient", _FakeWhopClient)
    sync = WhopEmailSync(
        verification_db=_FakeVerificationDB(),
        api_key="k",
        company_id="c",
        members_db=members_db,
    )
    return await sync.run_once()


async def _period_end(members_db, user_id):
    async with members_db._conn.execute(
        "SELECT current_period_end FROM whop_members WHERE whop_user_id = ?",
        (user_id,),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


@pytest.mark.asyncio
async def test_sync_writes_period_end_for_valid_paid_member(
    monkeypatch, members_db,
):
    await _run_sync(monkeypatch, members_db, [
        _member("u_paid", valid=True, period_end=1793000000),
    ])
    assert await _period_end(members_db, "u_paid") == 1793000000


@pytest.mark.asyncio
async def test_free_row_does_not_wipe_paid_rows_date(monkeypatch, members_db):
    # Same user walks twice: paid membership first, then a free one (null
    # renewal). The free row must leave the stored date alone.
    await _run_sync(monkeypatch, members_db, [
        _member("u_both", valid=True, period_end=1793000000,
                membership_id="mem_paid"),
        _member("u_both", valid=True, period_end=0,
                membership_id="mem_free"),
    ])
    assert await _period_end(members_db, "u_both") == 1793000000


@pytest.mark.asyncio
async def test_invalid_member_does_not_write_period_end(
    monkeypatch, members_db,
):
    await _run_sync(monkeypatch, members_db, [
        _member("u_lapsed", valid=False, period_end=1793000000),
    ])
    assert await _period_end(members_db, "u_lapsed") == 0
