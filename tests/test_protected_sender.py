"""Tests for the ProtectedSender pre-send gate.

Covers the four invariants the worker relies on:

  1. Suppressed recipients return ok=False without invoking the inner
     ResendClient (Resend quota not spent).
  2. Recipients inside the throttle window return ok=False with
     "throttled" in the error.
  3. Clean recipients pass through to the inner client untouched.
  4. The unsubscribe_url kwarg is forwarded to the inner client so the
     RFC 8058 List-Unsubscribe header lands on the outgoing message.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.email_bot.db import EmailDB, Subscriber
from src.email_bot.events_db import EmailEventsDB
from src.email_bot.protected_sender import ProtectedSender
from src.email_bot.sender import SendResult


@pytest_asyncio.fixture
async def events_db(tmp_path: Path):
    db = EmailEventsDB(db_path=str(tmp_path / "events.db"))
    await db.open()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def email_db(tmp_path: Path):
    db = EmailDB(db_path=str(tmp_path / "email.db"))
    await db.open()
    yield db
    await db.close()


def _fake_client(resend_id: str = "rs_123") -> AsyncMock:
    client = AsyncMock()
    client.send = AsyncMock(return_value=SendResult(ok=True, resend_id=resend_id))
    return client


@pytest.mark.asyncio
async def test_suppressed_recipient_blocks_send(events_db, email_db):
    await events_db.record_event(
        resend_email_id="re_h1",
        broadcast_id="",
        recipient="bouncer@x.com",
        event_type="bounced",
        event_at=int(time.time()),
        bounce_type="hard",
        raw_payload="{}",
    )
    client = _fake_client()
    sender = ProtectedSender(client=client, events_db=events_db, email_db=email_db)

    result = await sender.send(
        to="bouncer@x.com",
        subject="hi",
        html="<p>hi</p>",
        text="hi",
    )

    assert result.ok is False
    assert "suppressed" in (result.error or "")
    client.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_throttled_recipient_blocks_send(events_db, email_db):
    # Seed a recent successful send so last_sent_at sits inside the
    # default 30-minute throttle window.
    email = "throttled@x.com"
    await email_db.upsert_subscriber(Subscriber(
        email=email, name="X", trigger_type="cancellation",
        exit_reason="other", rejoin_url="u", created_at=int(time.time()),
    ))
    send_id = await email_db.schedule_one(
        email=email, sequence="winback", day=1, due_at=int(time.time()),
    )
    await email_db.mark_sent(send_id, resend_id="rs_prev")

    client = _fake_client()
    sender = ProtectedSender(
        client=client, events_db=events_db, email_db=email_db,
    )

    result = await sender.send(
        to=email, subject="hi", html="<p>hi</p>", text="hi",
    )

    assert result.ok is False
    assert "throttled" in (result.error or "")
    client.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_clean_recipient_delegates_to_inner_client(events_db, email_db):
    client = _fake_client(resend_id="rs_ok")
    sender = ProtectedSender(
        client=client, events_db=events_db, email_db=email_db,
    )

    result = await sender.send(
        to="fresh@x.com", subject="s", html="<p>h</p>", text="t",
    )

    assert result.ok is True
    assert result.resend_id == "rs_ok"
    client.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_unsubscribe_url_forwarded_to_inner_client(events_db, email_db):
    client = _fake_client()
    sender = ProtectedSender(
        client=client, events_db=events_db, email_db=email_db,
    )

    await sender.send(
        to="fresh@x.com",
        subject="s",
        html="<p>h</p>",
        text="t",
        unsubscribe_url="https://example.com/u/abc123",
        from_name="Potion",
        reply_to="hello@potionalpha.com",
    )

    client.send.assert_awaited_once()
    kwargs = client.send.await_args.kwargs
    assert kwargs["unsubscribe_url"] == "https://example.com/u/abc123"
    assert kwargs["from_name"] == "Potion"
    assert kwargs["reply_to"] == "hello@potionalpha.com"


@pytest.mark.asyncio
async def test_throttle_window_zero_disables_throttle(events_db, email_db):
    email = "freq@x.com"
    await email_db.upsert_subscriber(Subscriber(
        email=email, name="X", trigger_type="cancellation",
        exit_reason="other", rejoin_url="u", created_at=int(time.time()),
    ))
    send_id = await email_db.schedule_one(
        email=email, sequence="winback", day=1, due_at=int(time.time()),
    )
    await email_db.mark_sent(send_id, resend_id="rs_prev")

    client = _fake_client()
    sender = ProtectedSender(
        client=client, events_db=events_db, email_db=email_db,
        throttle_window_sec=0,
    )

    result = await sender.send(
        to=email, subject="s", html="<p>h</p>", text="t",
    )

    assert result.ok is True
    client.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_recipient_returns_error(events_db, email_db):
    client = _fake_client()
    sender = ProtectedSender(
        client=client, events_db=events_db, email_db=email_db,
    )
    result = await sender.send(to="", subject="s", html="<p>h</p>", text="t")
    assert result.ok is False
    client.send.assert_not_awaited()
