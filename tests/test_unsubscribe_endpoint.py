"""Tests for the GET /unsubscribe handler.

Verifies token issuance + verification, idempotency, opted-in flip on
whop_members, and that bad / missing tokens are rejected without leaking
info.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
import pytest_asyncio

from src.automations.whop_members_db import WhopMembersDB
from src.email_bot.events_db import EmailEventsDB
from src.email_bot.webhook import (
    EmailWebhookHandlers,
    compute_unsub_token,
    _verify_unsub_token,
)


_SECRET = "test-unsub-secret-do-not-use-in-prod"


@pytest_asyncio.fixture
async def events_db(tmp_path: Path):
    db = EmailEventsDB(db_path=str(tmp_path / "email_events.db"))
    await db.open()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def members_db(tmp_path: Path):
    db = WhopMembersDB(db_path=str(tmp_path / "whop_members.db"))
    await db.open()
    yield db
    await db.close()


def _make_handlers(events_db, members_db) -> EmailWebhookHandlers:
    return EmailWebhookHandlers(
        db=object(),
        whop_webhook_secret="x",
        admin_secret="y",
        rejoin_url_default="https://whop.com/potion",
        whop_members_db=members_db,
        events_db=events_db,
        email_unsub_secret=_SECRET,
        public_base_url="https://bot.example.com",
    )


def _make_request(query: dict, headers: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        query=query,
        headers=headers or {},
        remote="127.0.0.1",
    )


class TestTokenHelpers:
    def test_token_round_trip(self):
        email = "user@example.com"
        token = compute_unsub_token(_SECRET, email)
        assert token
        assert len(token) == 22  # 16 bytes urlsafe-b64 stripped of padding
        assert _verify_unsub_token(_SECRET, email, token) is True

    def test_token_case_insensitive_email(self):
        # Tokens are computed against lower(email) so different cases
        # of the same address validate.
        token = compute_unsub_token(_SECRET, "User@Example.COM")
        assert _verify_unsub_token(_SECRET, "user@example.com", token)

    def test_token_rejects_wrong_email(self):
        token = compute_unsub_token(_SECRET, "user@example.com")
        assert _verify_unsub_token(_SECRET, "attacker@example.com", token) is False

    def test_token_rejects_wrong_secret(self):
        token = compute_unsub_token(_SECRET, "user@example.com")
        assert _verify_unsub_token("other-secret", "user@example.com", token) is False

    def test_token_rejects_empty(self):
        assert _verify_unsub_token(_SECRET, "user@example.com", "") is False
        assert _verify_unsub_token(_SECRET, "", "anything") is False
        assert _verify_unsub_token("", "user@example.com", "x") is False


@pytest.mark.asyncio
class TestUnsubscribeEndpoint:
    async def test_valid_unsub_records_and_opts_out(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
    ):
        await members_db.upsert_member(
            "wuid-1", discord_user_id="d1",
            email="user@example.com", valid=True, membership_id="m1",
        )
        h = _make_handlers(events_db, members_db)
        token = compute_unsub_token(_SECRET, "user@example.com")
        request = _make_request({
            "e": "user@example.com",
            "t": token,
            "s": "winback_day1",
            "id": "re_xyz",
        })

        resp = await h._unsubscribe(request)
        assert resp.status == 200
        assert b"unsubscribed" in resp.body or "unsubscribed" in resp.text

        # Recorded in events_db.
        assert await events_db.is_unsubscribed("user@example.com") is True
        # Opted out in whop_members.
        assert await members_db.is_opted_out("user@example.com") is True

    async def test_idempotent_replay(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
    ):
        await members_db.upsert_member(
            "wuid-1", discord_user_id="d1",
            email="user@example.com", valid=True, membership_id="m1",
        )
        h = _make_handlers(events_db, members_db)
        token = compute_unsub_token(_SECRET, "user@example.com")
        request = _make_request({
            "e": "user@example.com",
            "t": token,
        })

        resp1 = await h._unsubscribe(request)
        resp2 = await h._unsubscribe(request)
        assert resp1.status == 200
        assert resp2.status == 200

        # Only one row in email_unsubscribes despite two requests.
        async with aiosqlite.connect(events_db._db_path) as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM email_unsubscribes "
                "WHERE recipient = ?",
                ("user@example.com",),
            ) as cur:
                row = await cur.fetchone()
        assert row[0] == 1

    async def test_bad_token_rejected(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
    ):
        await members_db.upsert_member(
            "wuid-1", discord_user_id="d1",
            email="user@example.com", valid=True, membership_id="m1",
        )
        h = _make_handlers(events_db, members_db)
        request = _make_request({
            "e": "user@example.com",
            "t": "ZmFrZS10b2tlbg==",  # not a valid HMAC
        })

        resp = await h._unsubscribe(request)
        assert resp.status == 400
        # Did NOT record an unsubscribe.
        assert await events_db.is_unsubscribed("user@example.com") is False
        # Did NOT flip opted_in.
        assert await members_db.is_opted_out("user@example.com") is False

    async def test_missing_email_rejected(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
    ):
        h = _make_handlers(events_db, members_db)
        request = _make_request({"t": "anytoken"})
        resp = await h._unsubscribe(request)
        assert resp.status == 400

    async def test_missing_token_rejected(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
    ):
        h = _make_handlers(events_db, members_db)
        request = _make_request({"e": "user@example.com"})
        resp = await h._unsubscribe(request)
        assert resp.status == 400

    async def test_attacker_cannot_unsub_other_email(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
    ):
        # Attacker signs a token for their own address, then tries to
        # use it to unsubscribe someone else. Must fail.
        await members_db.upsert_member(
            "wuid-victim", discord_user_id="d1",
            email="victim@example.com", valid=True, membership_id="m1",
        )
        h = _make_handlers(events_db, members_db)
        attacker_token = compute_unsub_token(_SECRET, "attacker@example.com")
        request = _make_request({
            "e": "victim@example.com",
            "t": attacker_token,
        })

        resp = await h._unsubscribe(request)
        assert resp.status == 400
        assert await events_db.is_unsubscribed("victim@example.com") is False
        assert await members_db.is_opted_out("victim@example.com") is False

    async def test_no_secret_rejects_all(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
    ):
        # Handler with empty unsub secret must reject every request,
        # even ones that look valid.
        h = EmailWebhookHandlers(
            db=object(),
            whop_webhook_secret="x",
            admin_secret="y",
            rejoin_url_default="https://whop.com/potion",
            whop_members_db=members_db,
            events_db=events_db,
            email_unsub_secret="",  # missing
            public_base_url="https://bot.example.com",
        )
        request = _make_request({
            "e": "user@example.com",
            "t": "any-token",
        })
        resp = await h._unsubscribe(request)
        assert resp.status == 400


@pytest.mark.asyncio
class TestSuppressionWritesReason:
    """Regression: _maybe_suppress now writes suppressed_at +
    suppressed_reason on the whop_members row."""

    async def test_complaint_records_reason(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
    ):
        await members_db.upsert_member(
            "wuid-1", discord_user_id="d1",
            email="hate@example.com", valid=True, membership_id="m1",
        )
        n = await members_db.mark_invalid_by_email(
            "hate@example.com", reason="complained",
        )
        assert n == 1
        async with aiosqlite.connect(members_db._db_path) as conn:
            async with conn.execute(
                "SELECT suppressed_at, suppressed_reason "
                "FROM whop_members WHERE whop_user_id = ?",
                ("wuid-1",),
            ) as cur:
                row = await cur.fetchone()
        assert row[0] > 0
        assert row[1] == "complained"

    async def test_repeat_suppression_is_noop(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
    ):
        await members_db.upsert_member(
            "wuid-1", discord_user_id="d1",
            email="hate@example.com", valid=True, membership_id="m1",
        )
        await members_db.mark_invalid_by_email(
            "hate@example.com", reason="hard_bounce",
        )
        # Second call returns 0 (already valid=0 → predicate skips).
        n = await members_db.mark_invalid_by_email(
            "hate@example.com", reason="complained",
        )
        assert n == 0
        # Original reason preserved (not overwritten).
        async with aiosqlite.connect(members_db._db_path) as conn:
            async with conn.execute(
                "SELECT suppressed_reason FROM whop_members "
                "WHERE whop_user_id = ?",
                ("wuid-1",),
            ) as cur:
                row = await cur.fetchone()
        assert row[0] == "hard_bounce"
