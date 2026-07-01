"""Tests for Blofin creds store + autotrade prefs (dedupe, daily cap)."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from src.trading.autotrade_prefs_db import AutotradePrefsDB
from src.trading.blofin_creds_db import BlofinCredsDB


@pytest_asyncio.fixture
async def creds_db(tmp_path: Path):
    db = BlofinCredsDB(db_path=str(tmp_path / "blofin_creds.db"))
    await db.open()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def prefs_db(tmp_path: Path):
    db = AutotradePrefsDB(db_path=str(tmp_path / "autotrade_prefs.db"))
    await db.open()
    yield db
    await db.close()


class TestBlofinCredsDB:
    @pytest.mark.asyncio
    async def test_upsert_and_decrypt_roundtrip(self, creds_db: BlofinCredsDB):
        await creds_db.upsert(111, "key-abc", "secret-xyz", "pass-123")
        creds = await creds_db.get_creds(111)
        assert creds is not None
        assert creds.api_key == "key-abc"
        assert creds.api_secret == "secret-xyz"
        assert creds.passphrase == "pass-123"

    @pytest.mark.asyncio
    async def test_deactivate_hides_creds(self, creds_db: BlofinCredsDB):
        await creds_db.upsert(111, "k", "s", "p")
        await creds_db.deactivate(111)
        assert await creds_db.get_creds(111) is None
        rec = await creds_db.get(111)
        assert rec is not None and rec.is_active is False

    @pytest.mark.asyncio
    async def test_delete_removes_row(self, creds_db: BlofinCredsDB):
        await creds_db.upsert(111, "k", "s", "p")
        await creds_db.delete(111)
        assert await creds_db.get(111) is None
        assert await creds_db.count_active() == 0

    @pytest.mark.asyncio
    async def test_count_active(self, creds_db: BlofinCredsDB):
        await creds_db.upsert(1, "k", "s", "p")
        await creds_db.upsert(2, "k", "s", "p")
        await creds_db.deactivate(2)
        assert await creds_db.count_active() == 1

    @pytest.mark.asyncio
    async def test_ciphertext_is_not_plaintext(self, creds_db: BlofinCredsDB, tmp_path):
        # Reconnect raw and confirm the stored blobs are not the plaintext.
        await creds_db.upsert(111, "key-abc", "secret-xyz", "pass-123")
        import aiosqlite
        async with aiosqlite.connect(str(tmp_path / "blofin_creds.db")) as conn:
            async with conn.execute(
                "SELECT api_key_encrypted, api_secret_encrypted, "
                "passphrase_encrypted FROM blofin_creds WHERE telegram_user_id=111"
            ) as cur:
                row = await cur.fetchone()
        assert "key-abc" not in row[0]
        assert "secret-xyz" not in row[1]
        assert "pass-123" not in row[2]


class TestAutotradePrefsDB:
    @pytest.mark.asyncio
    async def test_default_is_disabled(self, prefs_db: AutotradePrefsDB):
        prefs = await prefs_db.get_or_default(1, default_pct=5.0)
        assert prefs.enabled is False
        assert prefs.size_pct == 5.0
        assert prefs.ready is False

    @pytest.mark.asyncio
    async def test_ready_requires_enable_and_disclosure(self, prefs_db: AutotradePrefsDB):
        await prefs_db.set_enabled(1, True)
        assert (await prefs_db.get_or_default(1)).ready is False  # no disclosure
        await prefs_db.accept_disclosure(1)
        assert (await prefs_db.get_or_default(1)).ready is True

    @pytest.mark.asyncio
    async def test_size_pct_bounds(self, prefs_db: AutotradePrefsDB):
        await prefs_db.set_size_pct(1, 10.0)
        assert (await prefs_db.get_or_default(1)).size_pct == 10.0
        with pytest.raises(ValueError):
            await prefs_db.set_size_pct(1, 0.0)
        with pytest.raises(ValueError):
            await prefs_db.set_size_pct(1, 150.0)

    @pytest.mark.asyncio
    async def test_claim_fire_dedupes(self, prefs_db: AutotradePrefsDB):
        assert await prefs_db.try_claim_fire(1, 500) is True
        assert await prefs_db.try_claim_fire(1, 500) is False  # already fired
        assert await prefs_db.try_claim_fire(2, 500) is True   # other user ok
        assert await prefs_db.try_claim_fire(1, 501) is True   # other signal ok

    @pytest.mark.asyncio
    async def test_release_fire_allows_reclaim(self, prefs_db: AutotradePrefsDB):
        assert await prefs_db.try_claim_fire(1, 500) is True
        await prefs_db.release_fire(1, 500)
        assert await prefs_db.try_claim_fire(1, 500) is True

    @pytest.mark.asyncio
    async def test_daily_cap_and_reset(self, prefs_db: AutotradePrefsDB):
        day1 = 1_700_000_000       # 2023-11-14 UTC
        day2 = day1 + 86_400       # next day
        assert await prefs_db.try_consume_daily_slot(1, 2, now=day1) is True
        assert await prefs_db.try_consume_daily_slot(1, 2, now=day1) is True
        assert await prefs_db.try_consume_daily_slot(1, 2, now=day1) is False  # capped
        # New UTC day resets the counter.
        assert await prefs_db.try_consume_daily_slot(1, 2, now=day2) is True
