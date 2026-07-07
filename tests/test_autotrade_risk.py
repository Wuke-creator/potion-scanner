"""Tests for the pre-trade risk guard (passivbot-derived).

Uses a real AutotradePrefsDB (tmp) so peak tracking is exercised for real;
the venue client is mocked via a stub returning a fixed AccountSnapshot.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.config.settings import AutotradeConfig
from src.trading.autotrade_risk import AutotradeRiskGuard
from src.trading.autotrade_prefs_db import AutotradePrefsDB
from src.trading.hyperliquid_client import AccountSnapshot, TradePlan

UID = 111
ADDR = "0xabc"


def _plan(coin="BTC", notional=1000.0):
    return TradePlan(
        coin=coin, is_long=True, price=50000.0, size=notional / 50000.0,
        leverage=10, notional_usd=notional,
    )


def _config(**kw):
    base = dict(
        enabled=True,
        risk_symbol_we_limit=1.0,
        risk_total_we_limit=2.0,
        risk_max_drawdown_pct=15.0,
        risk_no_stacking=True,
    )
    base.update(kw)
    return AutotradeConfig(**base)


def _venue(snapshot):
    venue = SimpleNamespace()
    venue.get_account_snapshot = AsyncMock(return_value=snapshot)
    return venue


@pytest_asyncio.fixture
async def prefs_db(tmp_path: Path):
    db = AutotradePrefsDB(db_path=str(tmp_path / "autotrade_prefs.db"))
    await db.open()
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_allows_clean_account(prefs_db):
    guard = AutotradeRiskGuard(
        config=_config(),
        venue=_venue(AccountSnapshot(account_value=10_000.0, positions={})),
        prefs_db=prefs_db,
    )
    verdict = await guard.check(UID,_plan(notional=1000.0))
    assert verdict.allowed


@pytest.mark.asyncio
async def test_fails_closed_when_snapshot_unavailable(prefs_db):
    guard = AutotradeRiskGuard(
        config=_config(), venue=_venue(None), prefs_db=prefs_db,
    )
    verdict = await guard.check(UID,_plan())
    assert not verdict.allowed
    assert "risk data unavailable" in verdict.reason


@pytest.mark.asyncio
async def test_no_stacking_blocks_second_position_same_coin(prefs_db):
    snapshot = AccountSnapshot(account_value=10_000.0, positions={"BTC": 2_000.0})
    guard = AutotradeRiskGuard(config=_config(), venue=_venue(snapshot), prefs_db=prefs_db)
    verdict = await guard.check(UID,_plan(coin="BTC"))
    assert not verdict.allowed
    assert "already open" in verdict.reason


@pytest.mark.asyncio
async def test_no_stacking_disabled_falls_through_to_exposure(prefs_db):
    snapshot = AccountSnapshot(account_value=10_000.0, positions={"BTC": 2_000.0})
    guard = AutotradeRiskGuard(
        config=_config(risk_no_stacking=False),
        venue=_venue(snapshot),
        prefs_db=prefs_db,
    )
    # 2k existing + 1k new = 0.3x on a 10k account: fine.
    verdict = await guard.check(UID,_plan(coin="BTC", notional=1_000.0))
    assert verdict.allowed


@pytest.mark.asyncio
async def test_symbol_exposure_limit_blocks(prefs_db):
    snapshot = AccountSnapshot(account_value=10_000.0, positions={})
    guard = AutotradeRiskGuard(config=_config(), venue=_venue(snapshot), prefs_db=prefs_db)
    # 12k notional on a 10k account = 1.2x > 1.0x per-symbol limit.
    verdict = await guard.check(UID,_plan(notional=12_000.0))
    assert not verdict.allowed
    assert "per-symbol" in verdict.reason


@pytest.mark.asyncio
async def test_total_exposure_limit_blocks(prefs_db):
    # 16k open across other coins + 5k new = 2.1x > 2.0x total limit,
    # while the 5k trade alone (0.5x) passes the per-symbol check.
    snapshot = AccountSnapshot(
        account_value=10_000.0, positions={"ETH": 9_000.0, "SOL": 7_000.0},
    )
    guard = AutotradeRiskGuard(config=_config(), venue=_venue(snapshot), prefs_db=prefs_db)
    verdict = await guard.check(UID,_plan(coin="BTC", notional=5_000.0))
    assert not verdict.allowed
    assert "total wallet exposure" in verdict.reason


@pytest.mark.asyncio
async def test_drawdown_brake_blocks_below_peak(prefs_db):
    # Establish a 10k peak, then present an 8k account: 20% > 15% brake.
    await prefs_db.bump_equity_peak(UID, 10_000.0)
    snapshot = AccountSnapshot(account_value=8_000.0, positions={})
    guard = AutotradeRiskGuard(config=_config(), venue=_venue(snapshot), prefs_db=prefs_db)
    verdict = await guard.check(UID,_plan(notional=500.0))
    assert not verdict.allowed
    assert "below its" in verdict.reason


@pytest.mark.asyncio
async def test_drawdown_brake_releases_on_recovery(prefs_db):
    await prefs_db.bump_equity_peak(UID, 10_000.0)
    snapshot = AccountSnapshot(account_value=9_000.0, positions={})  # 10% below
    guard = AutotradeRiskGuard(config=_config(), venue=_venue(snapshot), prefs_db=prefs_db)
    verdict = await guard.check(UID,_plan(notional=500.0))
    assert verdict.allowed


@pytest.mark.asyncio
async def test_peak_ratchets_up_never_down(prefs_db):
    assert await prefs_db.bump_equity_peak(UID, 5_000.0) == 5_000.0
    assert await prefs_db.bump_equity_peak(UID, 12_000.0) == 12_000.0
    assert await prefs_db.bump_equity_peak(UID, 7_000.0) == 12_000.0


@pytest.mark.asyncio
async def test_zero_limits_disable_checks(prefs_db):
    snapshot = AccountSnapshot(
        account_value=10_000.0, positions={"ETH": 50_000.0},
    )
    guard = AutotradeRiskGuard(
        config=_config(
            risk_symbol_we_limit=0, risk_total_we_limit=0,
            risk_max_drawdown_pct=0, risk_no_stacking=False,
        ),
        venue=_venue(snapshot),
        prefs_db=prefs_db,
    )
    verdict = await guard.check(UID,_plan(notional=100_000.0))
    assert verdict.allowed
