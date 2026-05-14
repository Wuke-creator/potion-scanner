"""Tests for the trading delegates DB.

Covers the encryption round-trip and the upsert/get/delete lifecycle.
The Fernet master key is sourced from the standard auto-generated path
(``data/.encryption_key``) so we monkey-patch it onto a tmp_path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def _isolate_fernet(monkeypatch, tmp_path: Path):
    key = Fernet.generate_key()
    monkeypatch.setenv("ENCRYPTION_KEY", key.decode())
    from src import crypto
    crypto.reset_fernet()
    yield
    crypto.reset_fernet()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "delegates.db")


@pytest.mark.asyncio
async def test_round_trip_encryption(db_path: str):
    from src.trading.delegates_db import DelegatesDB

    db = DelegatesDB(db_path=db_path)
    await db.open()
    try:
        trader = "0x" + "ab" * 20
        delegate_key = "0x" + "cd" * 32
        await db.upsert(
            telegram_user_id=42,
            trader_address=trader,
            delegate_private_key=delegate_key,
        )
        record = await db.get(42)
        assert record is not None
        assert record.trader_address == trader
        assert record.delegate_private_key_encrypted != delegate_key, (
            "ciphertext must not equal plaintext"
        )
        plain = await db.get_plaintext_key(42)
        assert plain == delegate_key
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_upsert_overwrites(db_path: str):
    from src.trading.delegates_db import DelegatesDB

    db = DelegatesDB(db_path=db_path)
    await db.open()
    try:
        await db.upsert(
            telegram_user_id=1,
            trader_address="0x" + "11" * 20,
            delegate_private_key="0x" + "11" * 32,
        )
        await db.upsert(
            telegram_user_id=1,
            trader_address="0x" + "22" * 20,
            delegate_private_key="0x" + "22" * 32,
        )
        record = await db.get(1)
        assert record is not None
        assert record.trader_address == "0x" + "22" * 20
        plain = await db.get_plaintext_key(1)
        assert plain == "0x" + "22" * 32
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delete_wipes(db_path: str):
    from src.trading.delegates_db import DelegatesDB

    db = DelegatesDB(db_path=db_path)
    await db.open()
    try:
        await db.upsert(
            telegram_user_id=7,
            trader_address="0x" + "aa" * 20,
            delegate_private_key="0x" + "bb" * 32,
        )
        assert await db.get(7) is not None
        await db.delete(7)
        assert await db.get(7) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_plaintext_returns_none_when_inactive(db_path: str):
    from src.trading.delegates_db import DelegatesDB

    db = DelegatesDB(db_path=db_path)
    await db.open()
    try:
        await db.upsert(
            telegram_user_id=5,
            trader_address="0x" + "aa" * 20,
            delegate_private_key="0x" + "bb" * 32,
        )
        await db.deactivate(5)
        assert await db.get_plaintext_key(5) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_mark_trade_success_clears_prior_failure(db_path: str):
    from src.trading.delegates_db import DelegatesDB

    db = DelegatesDB(db_path=db_path)
    await db.open()
    try:
        await db.upsert(
            telegram_user_id=9,
            trader_address="0x" + "aa" * 20,
            delegate_private_key="0x" + "bb" * 32,
        )
        await db.mark_trade_failure(9, "insufficient USDC")
        record = await db.get(9)
        assert record is not None
        assert record.last_failure_reason == "insufficient USDC"

        await db.mark_trade_success(9)
        record = await db.get(9)
        assert record is not None
        assert record.last_failure_reason is None
        assert record.last_failure_at is None
        assert record.last_trade_at is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_count_active(db_path: str):
    from src.trading.delegates_db import DelegatesDB

    db = DelegatesDB(db_path=db_path)
    await db.open()
    try:
        assert await db.count_active() == 0
        for uid in (1, 2, 3):
            await db.upsert(
                telegram_user_id=uid,
                trader_address="0x" + "aa" * 20,
                delegate_private_key="0x" + "bb" * 32,
            )
        assert await db.count_active() == 3
        await db.deactivate(2)
        assert await db.count_active() == 2
    finally:
        await db.close()
