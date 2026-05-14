"""Tests for per-user trading settings (slippage + size presets)."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "user_settings.db")


@pytest.mark.asyncio
async def test_defaults_for_new_user(db_path: str):
    from src.trading.user_settings_db import (
        DEFAULT_SIZE_PRESETS,
        DEFAULT_SLIPPAGE_BPS,
        UserTradingSettingsDB,
    )

    db = UserTradingSettingsDB(db_path=db_path)
    await db.open()
    try:
        s = await db.get_or_default(123)
        assert s.slippage_bps == DEFAULT_SLIPPAGE_BPS
        assert s.size_presets == [float(p) for p in DEFAULT_SIZE_PRESETS]
        assert s.last_used_size is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_set_slippage_round_trip(db_path: str):
    from src.trading.user_settings_db import UserTradingSettingsDB

    db = UserTradingSettingsDB(db_path=db_path)
    await db.open()
    try:
        await db.set_slippage(7, 35)
        s = await db.get_or_default(7)
        assert s.slippage_bps == 35
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_set_slippage_rejects_out_of_range(db_path: str):
    from src.trading.user_settings_db import UserTradingSettingsDB

    db = UserTradingSettingsDB(db_path=db_path)
    await db.open()
    try:
        with pytest.raises(ValueError):
            await db.set_slippage(7, 0)
        with pytest.raises(ValueError):
            await db.set_slippage(7, 99999)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_set_presets_dedupes_and_sorts(db_path: str):
    from src.trading.user_settings_db import UserTradingSettingsDB

    db = UserTradingSettingsDB(db_path=db_path)
    await db.open()
    try:
        await db.set_presets(11, [250, 25, 50, 25, 100])
        s = await db.get_or_default(11)
        assert s.size_presets == [25.0, 50.0, 100.0, 250.0]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_set_presets_rejects_empty_and_negative(db_path: str):
    from src.trading.user_settings_db import UserTradingSettingsDB

    db = UserTradingSettingsDB(db_path=db_path)
    await db.open()
    try:
        with pytest.raises(ValueError):
            await db.set_presets(11, [])
        with pytest.raises(ValueError):
            await db.set_presets(11, [-5, 25])
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_set_presets_respects_max_count(db_path: str):
    from src.trading.user_settings_db import MAX_PRESETS, UserTradingSettingsDB

    db = UserTradingSettingsDB(db_path=db_path)
    await db.open()
    try:
        with pytest.raises(ValueError):
            await db.set_presets(
                1, [float(i + 1) for i in range(MAX_PRESETS + 1)],
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_mark_size_used(db_path: str):
    from src.trading.user_settings_db import UserTradingSettingsDB

    db = UserTradingSettingsDB(db_path=db_path)
    await db.open()
    try:
        await db.mark_size_used(99, 175.0)
        s = await db.get_or_default(99)
        assert s.last_used_size == 175.0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_default_last_used_leverage_is_none(db_path: str):
    from src.trading.user_settings_db import UserTradingSettingsDB

    db = UserTradingSettingsDB(db_path=db_path)
    await db.open()
    try:
        s = await db.get_or_default(7)
        assert s.last_used_leverage is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_set_last_used_leverage_round_trip(db_path: str):
    from src.trading.user_settings_db import UserTradingSettingsDB

    db = UserTradingSettingsDB(db_path=db_path)
    await db.open()
    try:
        await db.set_last_used_leverage(7, 12)
        s = await db.get_or_default(7)
        assert s.last_used_leverage == 12
        # Updating preserves slippage + presets (each setter only touches its column).
        await db.set_last_used_leverage(7, 25)
        s2 = await db.get_or_default(7)
        assert s2.last_used_leverage == 25
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_set_last_used_leverage_rejects_out_of_range(db_path: str):
    from src.trading.user_settings_db import UserTradingSettingsDB

    db = UserTradingSettingsDB(db_path=db_path)
    await db.open()
    try:
        with pytest.raises(ValueError):
            await db.set_last_used_leverage(1, 0)
        with pytest.raises(ValueError):
            await db.set_last_used_leverage(1, 9999)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_independent_users_do_not_collide(db_path: str):
    from src.trading.user_settings_db import UserTradingSettingsDB

    db = UserTradingSettingsDB(db_path=db_path)
    await db.open()
    try:
        await db.set_slippage(1, 10)
        await db.set_slippage(2, 200)
        await db.set_presets(1, [25])
        await db.set_presets(2, [1000, 500])
        s1 = await db.get_or_default(1)
        s2 = await db.get_or_default(2)
        assert s1.slippage_bps == 10
        assert s2.slippage_bps == 200
        assert s1.size_presets == [25.0]
        assert s2.size_presets == [500.0, 1000.0]
    finally:
        await db.close()
