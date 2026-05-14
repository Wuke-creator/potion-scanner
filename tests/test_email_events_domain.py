"""Tests for the recipient_domain column added to email_events.

Verifies:
  - record_event populates recipient_domain on insert
  - schema migration is idempotent (re-open of an already-migrated DB
    does not raise)
  - one-shot backfill on open populates rows that pre-date the column
  - email_unsubscribes table writes are idempotent (UNIQUE on recipient)
  - is_unsubscribed lookup is correct, case-insensitive
"""

from __future__ import annotations

import time
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from src.email_bot.events_db import EmailEventsDB, _domain_from_email


@pytest_asyncio.fixture
async def events_db(tmp_path: Path):
    db = EmailEventsDB(db_path=str(tmp_path / "email_events.db"))
    await db.open()
    yield db
    await db.close()


class TestDomainExtraction:
    def test_simple(self):
        assert _domain_from_email("user@example.com") == "example.com"

    def test_lowercases(self):
        assert _domain_from_email("USER@Example.COM") == "example.com"

    def test_subdomain(self):
        assert _domain_from_email("a@mail.example.co.uk") == "mail.example.co.uk"

    def test_empty(self):
        assert _domain_from_email("") == ""

    def test_no_at(self):
        assert _domain_from_email("notanemail") == ""

    def test_trailing_at(self):
        assert _domain_from_email("user@") == ""


@pytest.mark.asyncio
class TestEventsDomainColumn:
    async def test_record_event_populates_domain(self, events_db: EmailEventsDB):
        await events_db.record_event(
            resend_email_id="e1",
            broadcast_id="",
            recipient="alice@gmail.com",
            event_type="delivered",
            event_at=int(time.time()),
            raw_payload="{}",
        )
        async with aiosqlite.connect(events_db._db_path) as conn:
            async with conn.execute(
                "SELECT recipient_domain FROM email_events WHERE resend_email_id = ?",
                ("e1",),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        assert row[0] == "gmail.com"

    async def test_record_event_handles_uppercase(
        self, events_db: EmailEventsDB,
    ):
        await events_db.record_event(
            resend_email_id="e2",
            broadcast_id="",
            recipient="BOB@YAHOO.COM",
            event_type="delivered",
            event_at=int(time.time()),
            raw_payload="{}",
        )
        async with aiosqlite.connect(events_db._db_path) as conn:
            async with conn.execute(
                "SELECT recipient_domain FROM email_events WHERE resend_email_id = ?",
                ("e2",),
            ) as cur:
                row = await cur.fetchone()
        assert row[0] == "yahoo.com"

    async def test_idempotent_open(self, tmp_path: Path):
        # Open + close + open again. The migration should swallow
        # "duplicate column" and the backfill should skip already-domained rows.
        path = str(tmp_path / "events.db")
        db1 = EmailEventsDB(db_path=path)
        await db1.open()
        await db1.record_event(
            resend_email_id="e1",
            broadcast_id="",
            recipient="alice@gmail.com",
            event_type="delivered",
            event_at=int(time.time()),
            raw_payload="{}",
        )
        await db1.close()

        db2 = EmailEventsDB(db_path=path)
        await db2.open()  # Should not raise
        try:
            # Original row still has its domain.
            async with aiosqlite.connect(path) as conn:
                async with conn.execute(
                    "SELECT recipient_domain FROM email_events"
                ) as cur:
                    rows = await cur.fetchall()
            assert rows == [("gmail.com",)]
        finally:
            await db2.close()

    async def test_backfill_populates_legacy_rows(self, tmp_path: Path):
        # Simulate the pre-migration schema: insert a row directly with no
        # recipient_domain column, then open with the new EmailEventsDB and
        # check the backfill ran.
        path = str(tmp_path / "events.db")
        async with aiosqlite.connect(path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            # Old schema (pre-domain column).
            await conn.execute(
                """
                CREATE TABLE email_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  resend_email_id TEXT NOT NULL,
                  broadcast_id TEXT NOT NULL DEFAULT '',
                  recipient TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  event_at INTEGER NOT NULL,
                  click_url TEXT,
                  bounce_type TEXT,
                  bounce_message TEXT,
                  raw_payload TEXT NOT NULL,
                  UNIQUE(resend_email_id, event_type, event_at)
                )
                """
            )
            await conn.execute(
                "INSERT INTO email_events "
                "(resend_email_id, recipient, event_type, event_at, raw_payload) "
                "VALUES (?, ?, ?, ?, ?)",
                ("legacy-1", "Old@Example.COM", "delivered",
                 int(time.time()), "{}"),
            )
            await conn.commit()

        db = EmailEventsDB(db_path=path)
        await db.open()
        try:
            async with aiosqlite.connect(path) as conn:
                async with conn.execute(
                    "SELECT recipient_domain FROM email_events "
                    "WHERE resend_email_id = ?",
                    ("legacy-1",),
                ) as cur:
                    row = await cur.fetchone()
            assert row[0] == "example.com"
        finally:
            await db.close()


@pytest.mark.asyncio
class TestEmailUnsubscribesTable:
    async def test_record_unsubscribe(self, events_db: EmailEventsDB):
        ok = await events_db.record_unsubscribe(
            recipient="user@example.com",
            source="winback_day1",
            resend_email_id="re_abc",
            ip_address="1.2.3.4",
            user_agent="Mozilla/5.0",
        )
        assert ok is True
        assert await events_db.is_unsubscribed("user@example.com") is True
        # Case-insensitive.
        assert await events_db.is_unsubscribed("USER@example.com") is True
        # Different recipient, not unsubscribed.
        assert await events_db.is_unsubscribed("other@example.com") is False

    async def test_unsubscribe_is_idempotent(self, events_db: EmailEventsDB):
        first = await events_db.record_unsubscribe(
            recipient="user@example.com",
            source="onboarding_day0",
        )
        second = await events_db.record_unsubscribe(
            recipient="user@example.com",
            source="dunning_day3",  # different source on second click
        )
        assert first is True
        assert second is False  # already unsubscribed
        async with aiosqlite.connect(events_db._db_path) as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM email_unsubscribes WHERE recipient = ?",
                ("user@example.com",),
            ) as cur:
                row = await cur.fetchone()
        assert row[0] == 1

    async def test_unsubscribe_populates_domain(self, events_db: EmailEventsDB):
        await events_db.record_unsubscribe(recipient="alice@gmail.com")
        async with aiosqlite.connect(events_db._db_path) as conn:
            async with conn.execute(
                "SELECT recipient_domain FROM email_unsubscribes "
                "WHERE recipient = ?",
                ("alice@gmail.com",),
            ) as cur:
                row = await cur.fetchone()
        assert row[0] == "gmail.com"

    async def test_empty_recipient_returns_false(
        self, events_db: EmailEventsDB,
    ):
        ok = await events_db.record_unsubscribe(recipient="")
        assert ok is False
