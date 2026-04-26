"""Tests for the open_signals memory layer.

Covers:
  - Insert + lookup by symbol (full pair and bare base)
  - Insert + lookup by trade_id (more reliable when available)
  - Idempotent inserts when (channel_id, trade_id) match an existing row
  - Status flips and the "don't return terminal-status rows" rule
  - Channel scoping (signals don't bleed across channels)
  - Cleanup by age
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.automations.open_signals_db import OpenSignalsDB, _normalise_base


@pytest.mark.asyncio
async def test_normalise_base_strips_quote_and_leverage():
    assert _normalise_base("WET/USDT") == "WET"
    assert _normalise_base("ETH/USD 10x") == "ETH"
    assert _normalise_base("btc") == "BTC"
    assert _normalise_base("") == ""


@pytest.mark.asyncio
async def test_record_and_find_by_pair(tmp_path: Path):
    db = OpenSignalsDB(db_path=str(tmp_path / "open_signals.db"))
    await db.open()
    try:
        await db.record_signal(
            channel_id=42,
            pair="WET/USDT",
            side="SHORT",
            leverage=50,
            entry=0.099,
            stop_loss=0.105,
            tp1=0.094,
            tp2=0.090,
            tp3=0.085,
            trade_id=None,
            raw_message="raw post",
        )
        # Lookup by full pair works
        sig = await db.find_latest_open(channel_id=42, pair_or_base="WET/USDT")
        assert sig is not None
        assert sig.pair == "WET/USDT"
        assert sig.side == "SHORT"
        assert sig.leverage == 50
        assert sig.entry == pytest.approx(0.099)
        assert sig.stop_loss == pytest.approx(0.105)
        assert sig.tp1 == pytest.approx(0.094)
        assert sig.status == "open"

        # Lookup by bare base works (case-insensitive)
        sig2 = await db.find_latest_open(channel_id=42, pair_or_base="wet")
        assert sig2 is not None
        assert sig2.pair == "WET/USDT"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_idempotent_insert_on_trade_id(tmp_path: Path):
    db = OpenSignalsDB(db_path=str(tmp_path / "x.db"))
    await db.open()
    try:
        rid1 = await db.record_signal(
            channel_id=1, pair="ETH/USDT", side="LONG", leverage=10,
            entry=3000.0, stop_loss=2900.0, tp1=3100.0, tp2=3200.0,
            tp3=3300.0, trade_id=1234, raw_message="first",
        )
        rid2 = await db.record_signal(
            channel_id=1, pair="ETH/USDT", side="LONG", leverage=10,
            entry=3000.0, stop_loss=2900.0, tp1=3100.0, tp2=3200.0,
            tp3=3300.0, trade_id=1234, raw_message="dup",
        )
        # Same row id returned for the same (channel_id, trade_id)
        assert rid1 == rid2

        # Different channel: NEW row
        rid3 = await db.record_signal(
            channel_id=2, pair="ETH/USDT", side="LONG", leverage=10,
            entry=3000.0, stop_loss=2900.0, tp1=3100.0, tp2=3200.0,
            tp3=3300.0, trade_id=1234, raw_message="diff channel",
        )
        assert rid3 != rid1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_find_by_trade_id(tmp_path: Path):
    db = OpenSignalsDB(db_path=str(tmp_path / "x.db"))
    await db.open()
    try:
        await db.record_signal(
            channel_id=1, pair="BTC/USDT", side="LONG", leverage=5,
            entry=70000.0, stop_loss=68000.0, tp1=72000.0, tp2=74000.0,
            tp3=76000.0, trade_id=999, raw_message="r",
        )
        hit = await db.find_by_trade_id(channel_id=1, trade_id=999)
        assert hit is not None
        assert hit.pair == "BTC/USDT"
        assert hit.trade_id == 999

        miss = await db.find_by_trade_id(channel_id=1, trade_id=12345)
        assert miss is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_terminal_status_excludes_from_lookup(tmp_path: Path):
    db = OpenSignalsDB(db_path=str(tmp_path / "x.db"))
    await db.open()
    try:
        await db.record_signal(
            channel_id=1, pair="SOL/USDT", side="LONG", leverage=20,
            entry=100.0, stop_loss=95.0, tp1=110.0, tp2=120.0, tp3=130.0,
            trade_id=None, raw_message="r",
        )
        # Mark closed
        flipped = await db.update_status(
            channel_id=1, pair_or_base="SOL", new_status="closed",
        )
        assert flipped is True
        # Now find_latest_open returns nothing (terminal status)
        miss = await db.find_latest_open(channel_id=1, pair_or_base="SOL")
        assert miss is None

        # tp_hit (non-terminal) doesn't exclude
        await db.record_signal(
            channel_id=1, pair="SOL/USDT", side="LONG", leverage=20,
            entry=100.0, stop_loss=95.0, tp1=110.0, tp2=120.0, tp3=130.0,
            trade_id=None, raw_message="r2",
        )
        await db.update_status(
            channel_id=1, pair_or_base="SOL", new_status="tp_hit",
        )
        hit = await db.find_latest_open(channel_id=1, pair_or_base="SOL")
        assert hit is not None
        assert hit.status == "tp_hit"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_channel_scoping(tmp_path: Path):
    db = OpenSignalsDB(db_path=str(tmp_path / "x.db"))
    await db.open()
    try:
        await db.record_signal(
            channel_id=10, pair="WIF/USDT", side="LONG", leverage=1,
            entry=2.0, stop_loss=1.8, tp1=2.2, tp2=2.4, tp3=2.6,
            trade_id=None, raw_message="ch10",
        )
        # Looking up the same symbol on a different channel returns None
        miss = await db.find_latest_open(channel_id=20, pair_or_base="WIF")
        assert miss is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cleanup_older_than(tmp_path: Path):
    db = OpenSignalsDB(db_path=str(tmp_path / "x.db"))
    await db.open()
    try:
        # Insert one old, one fresh
        old_ts = int(time.time()) - 60 * 60 * 24 * 60  # 60d old
        await db.record_signal(
            channel_id=1, pair="OLD/USDT", side="LONG", leverage=1,
            entry=1.0, stop_loss=0.9, tp1=1.1, tp2=1.2, tp3=1.3,
            trade_id=None, raw_message="old", opened_at=old_ts,
        )
        await db.record_signal(
            channel_id=1, pair="NEW/USDT", side="LONG", leverage=1,
            entry=1.0, stop_loss=0.9, tp1=1.1, tp2=1.2, tp3=1.3,
            trade_id=None, raw_message="new",
        )
        # Prune anything older than 30 days
        deleted = await db.cleanup_older_than(max_age_seconds=30 * 86400)
        assert deleted == 1
        # NEW still findable
        assert await db.find_latest_open(
            channel_id=1, pair_or_base="NEW",
        ) is not None
        # OLD gone
        assert await db.find_latest_open(
            channel_id=1, pair_or_base="OLD",
        ) is None
    finally:
        await db.close()
