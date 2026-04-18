"""Tests for src/verification/db.py — aiosqlite store for verified users."""

from pathlib import Path

import pytest
import pytest_asyncio

from src.verification.db import VerificationDB


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    d = VerificationDB(db_path=str(tmp_path / "verified.db"))
    await d.open()
    yield d
    await d.close()


@pytest.mark.asyncio
class TestPendingVerifications:
    async def test_store_and_consume(self, db: VerificationDB):
        await db.store_pending(
            state="state-token-1", telegram_user_id=42, code_verifier="verifier-1",
        )
        row = await db.consume_pending("state-token-1")
        assert row is not None
        assert row.telegram_user_id == 42
        assert row.code_verifier == "verifier-1"

    async def test_consume_is_one_shot(self, db: VerificationDB):
        await db.store_pending("state-x", 1, "v")
        first = await db.consume_pending("state-x")
        second = await db.consume_pending("state-x")
        assert first is not None
        assert second is None

    async def test_consume_unknown_state_returns_none(self, db: VerificationDB):
        assert await db.consume_pending("does-not-exist") is None

    async def test_cleanup_expired_removes_old_rows(self, db: VerificationDB):
        import time

        await db.store_pending("old", 1, "v")
        # Force this row's created_at into the past by direct SQL
        await db._conn.execute(  # noqa: SLF001
            "UPDATE pending_verifications SET created_at = ? WHERE state = ?",
            (int(time.time()) - 9999, "old"),
        )
        await db._conn.commit()  # noqa: SLF001

        await db.store_pending("fresh", 2, "v")

        deleted = await db.cleanup_expired_pending(max_age_seconds=600)
        assert deleted == 1
        assert await db.consume_pending("old") is None
        assert await db.consume_pending("fresh") is not None


@pytest.mark.asyncio
class TestVerifiedUsers:
    async def test_upsert_creates_row(self, db: VerificationDB):
        await db.upsert_verified(
            telegram_user_id=42,
            discord_user_id="1111111111111111111",
            refresh_token_encrypted="encrypted-token",
        )
        record = await db.get_verified(42)
        assert record is not None
        assert record.discord_user_id == "1111111111111111111"
        assert record.refresh_token_encrypted == "encrypted-token"
        assert record.is_active is True

    async def test_upsert_updates_existing_row(self, db: VerificationDB):
        await db.upsert_verified(42, "discord_old", "old_token")
        await db.upsert_verified(42, "discord_new", "new_token")

        record = await db.get_verified(42)
        assert record is not None
        assert record.discord_user_id == "discord_new"
        assert record.refresh_token_encrypted == "new_token"

    async def test_get_unknown_user_returns_none(self, db: VerificationDB):
        assert await db.get_verified(99999) is None

    async def test_list_active_returns_only_active(self, db: VerificationDB):
        await db.upsert_verified(1, "d1", "t1")
        await db.upsert_verified(2, "d2", "t2")
        await db.upsert_verified(3, "d3", "t3")
        # Mark user 2 inactive
        await db.update_after_recheck(telegram_user_id=2, is_active=False)

        active = await db.list_active()
        ids = sorted(u.telegram_user_id for u in active)
        assert ids == [1, 3]

    async def test_list_active_user_ids_is_hot_path(self, db: VerificationDB):
        await db.upsert_verified(1, "d1", "t1")
        await db.upsert_verified(2, "d2", "t2")
        await db.update_after_recheck(telegram_user_id=2, is_active=False)
        ids = await db.list_active_user_ids()
        assert ids == [1]

    async def test_count_active(self, db: VerificationDB):
        await db.upsert_verified(1, "d1", "t1")
        await db.upsert_verified(2, "d2", "t2")
        assert await db.count_active() == 2
        await db.update_after_recheck(telegram_user_id=2, is_active=False)
        assert await db.count_active() == 1

    async def test_update_after_recheck_rotates_refresh_token(self, db: VerificationDB):
        await db.upsert_verified(42, "d", "old_token")
        await db.update_after_recheck(
            telegram_user_id=42,
            is_active=True,
            new_refresh_token_encrypted="new_token",
        )
        record = await db.get_verified(42)
        assert record is not None
        assert record.refresh_token_encrypted == "new_token"
        assert record.is_active is True

    async def test_update_after_recheck_can_mark_inactive(self, db: VerificationDB):
        await db.upsert_verified(42, "d", "t")
        await db.update_after_recheck(telegram_user_id=42, is_active=False)
        record = await db.get_verified(42)
        assert record is not None
        assert record.is_active is False
