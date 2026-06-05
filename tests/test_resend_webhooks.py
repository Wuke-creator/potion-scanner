"""Tests for the Resend webhook receiver, event store, and auto-suppression.

Mirrors the style of tests/test_email_bot.py: per-file pytest_asyncio
fixtures using tmp_path (real on-disk SQLite, no in-memory), helpers
tested directly without an aiohttp TestClient.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.automations.whop_members_db import WhopMembersDB
from src.email_bot.db import EmailDB, Subscriber
from src.email_bot.discord_commands import _render_broadcast_stats
from src.email_bot.events_db import EmailEventsDB
from src.email_bot.webhook import (
    EmailWebhookHandlers,
    _parse_resend_event,
    _resend_signature_ok,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Svix signature builder (test-only helper)
# ---------------------------------------------------------------------------


def _make_svix_headers(
    body: bytes,
    secret_b64: str,
    *,
    msg_id: str = "msg_2abc",
    timestamp: int | None = None,
    extra_signatures: list[str] | None = None,
) -> dict[str, str]:
    """Build a valid svix-{id,timestamp,signature} header set for `body`.

    secret_b64 is the base64 secret WITHOUT the 'whsec_' prefix; the
    test-side helper signs with the raw bytes that the production
    verifier will get after stripping the prefix.

    Pass extra_signatures to simulate Svix's key-rotation case where
    the header carries multiple v1,<base64> entries separated by spaces.
    """
    ts = int(time.time()) if timestamp is None else int(timestamp)
    secret_bytes = base64.b64decode(secret_b64)
    signed = f"{msg_id}.{ts}.".encode("utf-8") + body
    sig = base64.b64encode(
        hmac.new(secret_bytes, signed, hashlib.sha256).digest(),
    ).decode("ascii")
    pieces = [f"v1,{sig}"]
    if extra_signatures:
        pieces.extend(extra_signatures)
    return {
        "svix-id": msg_id,
        "svix-timestamp": str(ts),
        "svix-signature": " ".join(pieces),
    }


_TEST_SECRET_B64 = base64.b64encode(b"super-secret-resend-key-32bytes!").decode()
_TEST_SECRET_FULL = "whsec_" + _TEST_SECRET_B64


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


class TestResendSignature:
    def test_valid_signature_passes(self):
        body = b'{"type":"email.delivered","data":{"email_id":"abc"}}'
        headers = _make_svix_headers(body, _TEST_SECRET_B64)
        assert _resend_signature_ok(body, headers, _TEST_SECRET_FULL) is True

    def test_valid_signature_without_whsec_prefix_passes(self):
        # Some users paste just the base64 part. Both should work.
        body = b'{"x":1}'
        headers = _make_svix_headers(body, _TEST_SECRET_B64)
        assert _resend_signature_ok(body, headers, _TEST_SECRET_B64) is True

    def test_wrong_signature_fails(self):
        body = b'{"x":1}'
        headers = _make_svix_headers(body, _TEST_SECRET_B64)
        # Tamper the signature.
        bad_sig = "v1," + base64.b64encode(b"definitely_wrong" * 2).decode()
        headers["svix-signature"] = bad_sig
        assert _resend_signature_ok(body, headers, _TEST_SECRET_FULL) is False

    def test_tampered_body_fails(self):
        body = b'{"x":1}'
        headers = _make_svix_headers(body, _TEST_SECRET_B64)
        tampered = b'{"x":2}'
        assert _resend_signature_ok(tampered, headers, _TEST_SECRET_FULL) is False

    def test_expired_timestamp_fails(self):
        body = b'{"x":1}'
        # 10 minutes in the past = outside the 5-minute tolerance window.
        old_ts = int(time.time()) - 10 * 60
        headers = _make_svix_headers(body, _TEST_SECRET_B64, timestamp=old_ts)
        assert _resend_signature_ok(body, headers, _TEST_SECRET_FULL) is False

    def test_future_timestamp_outside_window_fails(self):
        body = b'{"x":1}'
        # Far future also rejected (clock skew can go both ways).
        future_ts = int(time.time()) + 10 * 60
        headers = _make_svix_headers(
            body, _TEST_SECRET_B64, timestamp=future_ts,
        )
        assert _resend_signature_ok(body, headers, _TEST_SECRET_FULL) is False

    def test_missing_header_fails(self):
        body = b'{"x":1}'
        headers = _make_svix_headers(body, _TEST_SECRET_B64)
        del headers["svix-id"]
        assert _resend_signature_ok(body, headers, _TEST_SECRET_FULL) is False

    def test_empty_secret_fails(self):
        body = b'{"x":1}'
        headers = _make_svix_headers(body, _TEST_SECRET_B64)
        assert _resend_signature_ok(body, headers, "") is False

    def test_multi_signature_any_valid_passes(self):
        # Svix supports key rotation: header carries two signatures, one
        # from the old key, one from the new. If our secret matches
        # EITHER, accept.
        body = b'{"x":1}'
        garbage = "v1," + base64.b64encode(b"old_rotated_key" * 2).decode()
        headers = _make_svix_headers(
            body, _TEST_SECRET_B64, extra_signatures=[garbage],
        )
        # Reverse to put garbage first; valid one second.
        sigs = headers["svix-signature"].split()
        headers["svix-signature"] = " ".join([garbage, sigs[0]])
        assert _resend_signature_ok(body, headers, _TEST_SECRET_FULL) is True

    def test_case_insensitive_header_lookup(self):
        # Plain dict (not CIMultiDict) with capitalised keys must still
        # validate, mirroring how aiohttp passes CIMultiDict in prod.
        body = b'{"x":1}'
        headers_lower = _make_svix_headers(body, _TEST_SECRET_B64)
        headers = {k.title(): v for k, v in headers_lower.items()}
        assert _resend_signature_ok(body, headers, _TEST_SECRET_FULL) is True


# ---------------------------------------------------------------------------
# Event payload parser
# ---------------------------------------------------------------------------


class TestParseResendEvent:
    def test_minimal_delivered(self):
        payload = {
            "type": "email.delivered",
            "created_at": "2026-04-24T10:00:00.000Z",
            "data": {
                "email_id": "abc-123",
                "to": ["User@Example.com"],
            },
        }
        event = _parse_resend_event(payload)
        assert event is not None
        assert event.event_type == "delivered"
        assert event.email_id == "abc-123"
        assert event.recipient == "user@example.com"
        assert event.broadcast_id == ""
        assert event.click_url is None
        assert event.bounce_type is None
        assert event.event_at > 0

    def test_clicked_extracts_url(self):
        payload = {
            "type": "email.clicked",
            "created_at": "2026-04-24T10:00:00.000Z",
            "data": {
                "email_id": "abc-123",
                "to": ["a@b.com"],
                "broadcast_id": "bcast-1",
                "click": {"link": "https://discord.com/x"},
            },
        }
        event = _parse_resend_event(payload)
        assert event is not None
        assert event.event_type == "clicked"
        assert event.click_url == "https://discord.com/x"
        assert event.broadcast_id == "bcast-1"

    def test_bounced_extracts_bounce_fields(self):
        payload = {
            "type": "email.bounced",
            "created_at": "2026-04-24T10:00:00.000Z",
            "data": {
                "email_id": "abc-123",
                "to": ["a@b.com"],
                "bounce": {
                    "type": "Hard",
                    "subType": "InvalidRecipient",
                    "message": "Mailbox does not exist",
                },
            },
        }
        event = _parse_resend_event(payload)
        assert event is not None
        assert event.bounce_type == "hard"
        assert event.bounce_message == "Mailbox does not exist"

    def test_unknown_event_type_rejected(self):
        payload = {
            "type": "email.unknown_thing",
            "data": {"email_id": "x", "to": ["a@b.com"]},
        }
        assert _parse_resend_event(payload) is None

    def test_non_email_prefix_rejected(self):
        payload = {"type": "domain.created", "data": {}}
        assert _parse_resend_event(payload) is None

    def test_missing_email_id_rejected(self):
        payload = {
            "type": "email.delivered",
            "data": {"to": ["a@b.com"]},
        }
        assert _parse_resend_event(payload) is None

    def test_missing_recipient_rejected(self):
        payload = {
            "type": "email.delivered",
            "data": {"email_id": "abc"},
        }
        assert _parse_resend_event(payload) is None


# ---------------------------------------------------------------------------
# EmailEventsDB
# ---------------------------------------------------------------------------


def _record_kwargs(**overrides) -> dict:
    base = dict(
        resend_email_id="abc-123",
        broadcast_id="bcast-1",
        recipient="user@example.com",
        event_type="delivered",
        event_at=1714000000,
        click_url=None,
        bounce_type=None,
        bounce_message=None,
        raw_payload="{}",
    )
    base.update(overrides)
    return base


@pytest.mark.asyncio
class TestEmailEventsIdempotency:
    async def test_same_event_twice_inserts_once(self, events_db: EmailEventsDB):
        first = await events_db.record_event(**_record_kwargs())
        second = await events_db.record_event(**_record_kwargs())
        assert first is True
        assert second is False
        history = await events_db.recipient_history("user@example.com")
        assert len(history) == 1

    async def test_different_event_types_for_same_email_id_both_recorded(
        self, events_db: EmailEventsDB,
    ):
        await events_db.record_event(**_record_kwargs(event_type="sent"))
        await events_db.record_event(**_record_kwargs(event_type="delivered"))
        history = await events_db.recipient_history("user@example.com")
        assert {h["event_type"] for h in history} == {"sent", "delivered"}

    async def test_same_type_different_event_at_both_recorded(
        self, events_db: EmailEventsDB,
    ):
        # Edge case: a real second send (different event_at) is preserved.
        await events_db.record_event(**_record_kwargs(event_at=1714000000))
        await events_db.record_event(**_record_kwargs(event_at=1714000060))
        history = await events_db.recipient_history("user@example.com")
        assert len(history) == 2


@pytest.mark.asyncio
class TestEmailEventsColumnPopulation:
    async def test_clicked_records_click_url(self, events_db: EmailEventsDB):
        await events_db.record_event(**_record_kwargs(
            event_type="clicked",
            click_url="https://discord.com/x",
        ))
        history = await events_db.recipient_history("user@example.com")
        assert history[0]["click_url"] == "https://discord.com/x"
        assert history[0]["bounce_type"] is None

    async def test_bounced_records_bounce_fields(
        self, events_db: EmailEventsDB,
    ):
        await events_db.record_event(**_record_kwargs(
            event_type="bounced",
            bounce_type="hard",
            bounce_message="Mailbox does not exist",
        ))
        history = await events_db.recipient_history("user@example.com")
        assert history[0]["bounce_type"] == "hard"
        assert history[0]["bounce_message"] == "Mailbox does not exist"
        assert history[0]["click_url"] is None

    async def test_opened_leaves_extras_null(self, events_db: EmailEventsDB):
        await events_db.record_event(**_record_kwargs(event_type="opened"))
        history = await events_db.recipient_history("user@example.com")
        assert history[0]["click_url"] is None
        assert history[0]["bounce_type"] is None


# ---------------------------------------------------------------------------
# broadcast_stats math
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBroadcastStats:
    async def _seed_clean_broadcast(self, events_db: EmailEventsDB) -> None:
        # 100 sent / 80 delivered / 40 opened / 8 clicked / 15 bounced
        # (12 hard, 3 soft) / 5 complaints / 5 failed for bcast-1.
        bcast = "bcast-1"
        ts = 1714000000

        async def insert(n, event_type, **extra):
            for i in range(n):
                await events_db.record_event(**_record_kwargs(
                    resend_email_id=f"{event_type}-{i}",
                    broadcast_id=bcast,
                    recipient=f"user{i}@example.com",
                    event_type=event_type,
                    event_at=ts + i,
                    **extra,
                ))

        await insert(100, "sent")
        await insert(80, "delivered")
        await insert(40, "opened")
        # Mix click URLs so top_clicked has predictable order.
        for i in range(8):
            url = "https://a/" if i < 5 else "https://b/"
            await events_db.record_event(**_record_kwargs(
                resend_email_id=f"clicked-{i}",
                broadcast_id=bcast,
                recipient=f"user{i}@example.com",
                event_type="clicked",
                event_at=ts + i,
                click_url=url,
            ))
        # 12 hard bounces.
        for i in range(12):
            await events_db.record_event(**_record_kwargs(
                resend_email_id=f"bounce-h-{i}",
                broadcast_id=bcast,
                recipient=f"hard{i}@example.com",
                event_type="bounced",
                event_at=ts + i,
                bounce_type="hard",
            ))
        # 3 soft bounces.
        for i in range(3):
            await events_db.record_event(**_record_kwargs(
                resend_email_id=f"bounce-s-{i}",
                broadcast_id=bcast,
                recipient=f"soft{i}@example.com",
                event_type="bounced",
                event_at=ts + i,
                bounce_type="soft",
            ))
        await insert(5, "complained")
        await insert(5, "failed")

    async def test_basic_counts_and_rates(self, events_db: EmailEventsDB):
        await self._seed_clean_broadcast(events_db)
        s = await events_db.broadcast_stats("bcast-1")
        assert s["sent"] == 100
        assert s["delivered"] == 80
        assert s["opened"] == 40
        assert s["clicked"] == 8
        assert s["bounced"] == 15
        assert s["hard_bounced"] == 12
        assert s["soft_bounced"] == 3
        assert s["complained"] == 5
        assert s["failed"] == 5
        assert s["delivery_rate"] == pytest.approx(0.80)
        assert s["open_rate"] == pytest.approx(0.50)
        assert s["click_rate_delivered"] == pytest.approx(0.10)
        assert s["click_rate_opened"] == pytest.approx(0.20)
        assert s["bounce_rate"] == pytest.approx(0.15)
        assert s["complaint_rate"] == pytest.approx(0.05)
        assert s["fail_rate"] == pytest.approx(0.05)

    async def test_top_clicked_urls_ordered_by_count_desc(
        self, events_db: EmailEventsDB,
    ):
        await self._seed_clean_broadcast(events_db)
        s = await events_db.broadcast_stats("bcast-1")
        urls = s["top_clicked_urls"]
        assert len(urls) == 2
        assert urls[0]["url"] == "https://a/"
        assert urls[0]["count"] == 5
        assert urls[1]["url"] == "https://b/"
        assert urls[1]["count"] == 3

    async def test_zero_delivered_avoids_divide_by_zero(
        self, events_db: EmailEventsDB,
    ):
        await events_db.record_event(**_record_kwargs(
            broadcast_id="empty", event_type="sent",
        ))
        s = await events_db.broadcast_stats("empty")
        assert s["delivery_rate"] == 0.0  # 0 delivered / 1 sent = 0
        assert s["open_rate"] == 0.0       # 0/0 guarded
        assert s["click_rate_delivered"] == 0.0
        assert s["click_rate_opened"] == 0.0

    async def test_other_broadcasts_excluded(self, events_db: EmailEventsDB):
        await self._seed_clean_broadcast(events_db)
        # Add events under a different broadcast that should NOT contaminate.
        for i in range(50):
            await events_db.record_event(**_record_kwargs(
                resend_email_id=f"other-{i}",
                broadcast_id="bcast-other",
                recipient=f"x{i}@example.com",
                event_type="delivered",
                event_at=1714000000 + i,
            ))
        s = await events_db.broadcast_stats("bcast-1")
        assert s["delivered"] == 80  # unchanged


# ---------------------------------------------------------------------------
# soft_bounce_count + hard_bounces_in_window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSoftBounceWindow:
    async def test_soft_bounce_count_respects_window(
        self, events_db: EmailEventsDB,
    ):
        now = int(time.time())
        # Two soft bounces inside the 30-day window, one outside.
        await events_db.record_event(**_record_kwargs(
            resend_email_id="s1", event_type="bounced",
            event_at=now - 1 * 86400, bounce_type="soft",
        ))
        await events_db.record_event(**_record_kwargs(
            resend_email_id="s2", event_type="bounced",
            event_at=now - 10 * 86400, bounce_type="soft",
        ))
        await events_db.record_event(**_record_kwargs(
            resend_email_id="s3", event_type="bounced",
            event_at=now - 60 * 86400, bounce_type="soft",
        ))
        cutoff = now - 30 * 86400
        n = await events_db.soft_bounce_count(
            "user@example.com", since_epoch=cutoff,
        )
        assert n == 2

    async def test_soft_bounce_count_ignores_hard_bounces(
        self, events_db: EmailEventsDB,
    ):
        now = int(time.time())
        await events_db.record_event(**_record_kwargs(
            resend_email_id="h1", event_type="bounced",
            event_at=now, bounce_type="hard",
        ))
        n = await events_db.soft_bounce_count(
            "user@example.com", since_epoch=0,
        )
        assert n == 0


# ---------------------------------------------------------------------------
# Auto-suppression in WhopMembersDB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMarkInvalidByEmail:
    async def test_flips_matching_row(self, members_db: WhopMembersDB):
        await members_db.upsert_member(
            "wuid-1", discord_user_id="d1", email="user@example.com",
            valid=True, membership_id="m1",
        )
        n = await members_db.mark_invalid_by_email("user@example.com")
        assert n == 1
        # Confirm via list_valid_with_email (the prod read path).
        rows = await members_db.list_valid_with_email()
        assert all(r.email != "user@example.com" for r in rows)

    async def test_flips_all_duplicates_with_same_email(
        self, members_db: WhopMembersDB,
    ):
        # Same email under two whop_user_ids (legitimate: multi-membership).
        await members_db.upsert_member(
            "wuid-1", discord_user_id="d1", email="dup@example.com",
            valid=True, membership_id="m1",
        )
        await members_db.upsert_member(
            "wuid-2", discord_user_id="d2", email="dup@example.com",
            valid=True, membership_id="m2",
        )
        n = await members_db.mark_invalid_by_email("dup@example.com")
        assert n == 2

    async def test_case_insensitive(self, members_db: WhopMembersDB):
        await members_db.upsert_member(
            "wuid-1", discord_user_id="d1", email="MixedCase@Example.com",
            valid=True, membership_id="m1",
        )
        n = await members_db.mark_invalid_by_email("mixedcase@example.com")
        assert n == 1

    async def test_repeat_call_is_noop(self, members_db: WhopMembersDB):
        await members_db.upsert_member(
            "wuid-1", discord_user_id="d1", email="user@example.com",
            valid=True, membership_id="m1",
        )
        first = await members_db.mark_invalid_by_email("user@example.com")
        second = await members_db.mark_invalid_by_email("user@example.com")
        assert first == 1
        assert second == 0  # already valid=0, predicate filters it out

    async def test_unknown_email_returns_zero(
        self, members_db: WhopMembersDB,
    ):
        n = await members_db.mark_invalid_by_email("nobody@example.com")
        assert n == 0

    async def test_empty_email_returns_zero(
        self, members_db: WhopMembersDB,
    ):
        assert await members_db.mark_invalid_by_email("") == 0


# ---------------------------------------------------------------------------
# End-to-end suppression via the webhook handler
# ---------------------------------------------------------------------------


def _resend_payload(
    *, event_type: str, recipient: str = "user@example.com",
    email_id: str = "rid-1", broadcast_id: str = "",
    bounce_type: str | None = None, click_url: str | None = None,
    created_at: str | None = None,
) -> dict:
    data: dict = {"email_id": email_id, "to": [recipient]}
    if broadcast_id:
        data["broadcast_id"] = broadcast_id
    if bounce_type is not None:
        data["bounce"] = {"type": bounce_type, "message": "test"}
    if click_url is not None:
        data["click"] = {"link": click_url}
    return {
        "type": f"email.{event_type}",
        "created_at": created_at or "2026-04-24T10:00:00.000Z",
        "data": data,
    }


def _signed_request(payload: dict, secret: str = _TEST_SECRET_FULL):
    """Build a SimpleNamespace that quacks like aiohttp's web.Request for
    the bits the handler touches: .read() and .headers."""
    body = json.dumps(payload).encode()
    headers = _make_svix_headers(body, _TEST_SECRET_B64)

    async def read():
        return body

    return SimpleNamespace(read=read, headers=headers), body


async def _make_handlers(
    events_db: EmailEventsDB,
    members_db: WhopMembersDB,
    email_db=None,
) -> EmailWebhookHandlers:
    # email_db is now used by _resend_webhook -> _maybe_suppress to
    # cancel pending sends on bounce/complaint. Default to an AsyncMock
    # for tests that don't care about the cancellation effect; pass a
    # real EmailDB for tests that assert against it.
    if email_db is None:
        email_db = AsyncMock()
        email_db.cancel_all_pending = AsyncMock(return_value=0)
    return EmailWebhookHandlers(
        db=email_db,
        whop_webhook_secret="ignored",
        admin_secret="ignored",
        rejoin_url_default="https://whop.com/potion",
        whop_members_db=members_db,
        resend_webhook_secret=_TEST_SECRET_FULL,
        events_db=events_db,
    )


@pytest.mark.asyncio
class TestResendWebhookSuppression:
    async def test_hard_bounce_suppresses(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
    ):
        await members_db.upsert_member(
            "wuid-1", discord_user_id="d1", email="user@example.com",
            valid=True, membership_id="m1",
        )
        handlers = await _make_handlers(events_db, members_db)
        request, _ = _signed_request(_resend_payload(
            event_type="bounced", bounce_type="hard",
        ))

        resp = await handlers._resend_webhook(request)
        assert resp.status == 200

        rows = await members_db.list_valid_with_email()
        assert len(rows) == 0

    async def test_complaint_suppresses(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
    ):
        await members_db.upsert_member(
            "wuid-1", discord_user_id="d1", email="user@example.com",
            valid=True, membership_id="m1",
        )
        handlers = await _make_handlers(events_db, members_db)
        request, _ = _signed_request(_resend_payload(event_type="complained"))

        await handlers._resend_webhook(request)
        rows = await members_db.list_valid_with_email()
        assert len(rows) == 0

    async def test_first_soft_bounce_does_not_suppress(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
    ):
        # 2026-05-25 policy: a lone soft bounce is treated as transient
        # (full mailbox, brief server issue, momentary DNS hiccup).
        # Suppression only fires once SOFT_BOUNCE_THRESHOLD_COUNT
        # strikes accumulate inside SOFT_BOUNCE_WINDOW_SECONDS.
        await members_db.upsert_member(
            "wuid-1", discord_user_id="d1", email="user@example.com",
            valid=True, membership_id="m1",
        )
        handlers = await _make_handlers(events_db, members_db)
        request, _ = _signed_request(_resend_payload(
            event_type="bounced", bounce_type="soft",
        ))

        await handlers._resend_webhook(request)
        rows = await members_db.list_valid_with_email()
        assert len(rows) == 1  # one soft bounce alone is not enough

    async def test_third_soft_bounce_in_window_suppresses(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
    ):
        await members_db.upsert_member(
            "wuid-1", discord_user_id="d1", email="user@example.com",
            valid=True, membership_id="m1",
        )
        handlers = await _make_handlers(events_db, members_db)

        # Three soft bounces inside the 14-day window trips the rule.
        # Each event needs a distinct (resend_email_id, event_type,
        # event_at) tuple so the UNIQUE constraint records each one.
        base_ts = int(time.time())
        for i in range(3):
            request, _ = _signed_request(_resend_payload(
                event_type="bounced", bounce_type="soft",
                email_id=f"soft-{i}",
                created_at=time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z",
                    time.gmtime(base_ts + i),
                ),
            ))
            await handlers._resend_webhook(request)

        rows = await members_db.list_valid_with_email()
        assert len(rows) == 0

    async def test_two_soft_bounces_in_window_do_not_suppress(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
    ):
        # Below the threshold inside the window. Stays sendable.
        await members_db.upsert_member(
            "wuid-1", discord_user_id="d1", email="user@example.com",
            valid=True, membership_id="m1",
        )
        handlers = await _make_handlers(events_db, members_db)
        base_ts = int(time.time())
        for i in range(2):
            request, _ = _signed_request(_resend_payload(
                event_type="bounced", bounce_type="soft",
                email_id=f"soft-{i}",
                created_at=time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z",
                    time.gmtime(base_ts + i),
                ),
            ))
            await handlers._resend_webhook(request)
        rows = await members_db.list_valid_with_email()
        assert len(rows) == 1

    async def test_old_soft_bounces_outside_window_do_not_count(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
    ):
        # Two soft bounces from 60 days ago plus one fresh one is below
        # the threshold inside the 14-day window. Stays sendable.
        await members_db.upsert_member(
            "wuid-1", discord_user_id="d1", email="user@example.com",
            valid=True, membership_id="m1",
        )
        old_ts = int(time.time()) - 60 * 86400
        for i in range(2):
            await events_db.record_event(**_record_kwargs(
                resend_email_id=f"old-{i}",
                event_type="bounced",
                event_at=old_ts + i,
                bounce_type="soft",
                recipient="user@example.com",
            ))

        handlers = await _make_handlers(events_db, members_db)
        request, _ = _signed_request(_resend_payload(
            event_type="bounced", bounce_type="soft", email_id="fresh-1",
        ))
        await handlers._resend_webhook(request)

        rows = await members_db.list_valid_with_email()
        assert len(rows) == 1  # historical bounces don't count

    async def test_retried_event_does_not_resuppress(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
    ):
        # Pre-suppress so we can detect any further updates.
        await members_db.upsert_member(
            "wuid-1", discord_user_id="d1", email="user@example.com",
            valid=True, membership_id="m1",
        )
        handlers = await _make_handlers(events_db, members_db)
        # Patch mark_invalid_by_email so we can count calls.
        members_db.mark_invalid_by_email = AsyncMock(  # type: ignore[method-assign]
            wraps=members_db.mark_invalid_by_email,
        )

        payload = _resend_payload(event_type="bounced", bounce_type="hard")
        # Same payload, same JSON ordering -> same bytes -> same signed sig.
        request1, body = _signed_request(payload)
        request2, _ = _signed_request(payload)

        await handlers._resend_webhook(request1)
        await handlers._resend_webhook(request2)

        # First insert flips the row; second is a UNIQUE collision so
        # mark_invalid_by_email must NOT be called the second time.
        assert members_db.mark_invalid_by_email.call_count == 1

    async def test_bounce_cancels_pending_scheduled_sends(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
        tmp_path: Path,
    ):
        # 2026-05-19 policy: when a bounce arrives we must not only
        # block FUTURE enrollment (whop_members.valid=0) but also
        # cancel any IN-FLIGHT scheduled rows so the worker does not
        # deliver another email to the bounced recipient.
        recipient = "bouncer@example.com"
        await members_db.upsert_member(
            "wuid-1", discord_user_id="d1", email=recipient,
            valid=True, membership_id="m1",
        )
        # Real on-disk EmailDB with a bronze sequence enqueued.
        email_db = EmailDB(db_path=str(tmp_path / "bounce_test_email.db"))
        await email_db.open()
        try:
            await email_db.upsert_subscriber(Subscriber(
                email=recipient, name="B", trigger_type="bronze",
                exit_reason="none", rejoin_url="u",
                created_at=int(time.time()),
            ))
            await email_db.schedule_sequence(
                email=recipient, sequence="bronze",
            )
            # All 5 bronze rows should be pending before the bounce.
            horizon = int(time.time()) + 30 * 86400
            pending_before = [
                s for s in await email_db.due_sends(now=horizon)
                if s.email == recipient and s.status == "pending"
            ]
            assert len(pending_before) == 5

            handlers = await _make_handlers(
                events_db, members_db, email_db=email_db,
            )
            request, _ = _signed_request(_resend_payload(
                event_type="bounced", bounce_type="hard",
                recipient=recipient,
            ))
            resp = await handlers._resend_webhook(request)
            assert resp.status == 200

            # whop_members invalidated AND every pending bronze row canceled.
            rows_valid = await members_db.list_valid_with_email()
            assert all(r.email != recipient for r in rows_valid)
            pending_after = [
                s for s in await email_db.due_sends(now=horizon)
                if s.email == recipient and s.status == "pending"
            ]
            assert pending_after == []
        finally:
            await email_db.close()

    async def test_unknown_recipient_no_crash(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
    ):
        handlers = await _make_handlers(events_db, members_db)
        request, _ = _signed_request(_resend_payload(
            event_type="bounced", bounce_type="hard",
            recipient="ghost@nowhere.example",
        ))
        resp = await handlers._resend_webhook(request)
        assert resp.status == 200

    async def test_bad_signature_returns_401(
        self, events_db: EmailEventsDB, members_db: WhopMembersDB,
    ):
        handlers = await _make_handlers(events_db, members_db)
        body = json.dumps(_resend_payload(event_type="delivered")).encode()
        bad_headers = {
            "svix-id": "msg_bad",
            "svix-timestamp": str(int(time.time())),
            "svix-signature": "v1," + base64.b64encode(b"wrong" * 8).decode(),
        }

        async def read():
            return body

        request = SimpleNamespace(read=read, headers=bad_headers)
        resp = await handlers._resend_webhook(request)
        assert resp.status == 401


# ---------------------------------------------------------------------------
# /email-broadcast-stats renderer
# ---------------------------------------------------------------------------


class TestRenderBroadcastStats:
    def _stats(self, **overrides) -> dict:
        base = {
            "sent": 121481,
            "delivered": 35807,
            "opened": 8234,
            "clicked": 1847,
            "bounced": 4302,
            "complained": 7,
            "failed": 23,
            "hard_bounced": 4201,
            "soft_bounced": 101,
            "delivery_rate": 35807 / 121481,
            "open_rate": 8234 / 35807,
            "click_rate_delivered": 1847 / 35807,
            "click_rate_opened": 1847 / 8234,
            "bounce_rate": 4302 / 121481,
            "complaint_rate": 7 / 121481,
            "fail_rate": 23 / 121481,
            "top_clicked_urls": [
                {"url": "https://discord.com/x", "count": 1205},
                {"url": "https://whop.com/potion", "count": 642},
            ],
        }
        base.update(overrides)
        return base

    def test_renders_expected_shape(self):
        body = _render_broadcast_stats("397edde3-uuid", "Potion 2.0", self._stats())
        # Header line uses a colon, not an em dash.
        assert 'Broadcast 397edde3-uuid: "Potion 2.0"' in body
        assert "—" not in body  # no em dashes anywhere
        # Big numbers carry comma separators.
        assert "121,481" in body
        assert "35,807" in body
        assert "4,201 hard / 101 soft" in body
        # Top CTAs section appears with both URLs.
        assert "Top CTAs:" in body
        assert "https://discord.com/x" in body
        assert "(1,205 clicks)" in body

    def test_no_top_ctas_section_when_empty(self):
        body = _render_broadcast_stats(
            "id", "Title", self._stats(top_clicked_urls=[]),
        )
        assert "Top CTAs" not in body

    def test_falls_back_on_empty_title(self):
        body = _render_broadcast_stats("id", "", self._stats())
        assert "(title unavailable)" in body
