"""Tests for the autotrade engine loop.

Uses a real AutotradePrefsDB (tmp) so dedupe + daily-cap behaviour is
exercised for real; the venue client, delegates store, and verification
store are mocked.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.config.settings import AutotradeConfig
from src.trading.autotrade_engine import AutotradeEngine
from src.trading.autotrade_prefs_db import AutotradePrefsDB
from src.trading.hyperliquid_client import AccountSnapshot, HyperliquidError, TradePlan

UID = 111
_DEFAULT_PLAN = TradePlan(
    coin="BTC", is_long=True, price=50000.0, size=0.02, leverage=10,
    notional_usd=1000.0,
)
_DEFAULT_RESULT = SimpleNamespace(
    coin="BTC", size=0.02, sl_ok=True, tp_ok=True, entry_oid=1, entry_avg_px=50000.0,
)
_SENTINEL = object()


@pytest_asyncio.fixture
async def prefs_db(tmp_path: Path):
    db = AutotradePrefsDB(db_path=str(tmp_path / "autotrade_prefs.db"))
    await db.open()
    yield db
    await db.close()


def _signal(**kw):
    base = dict(id=1, pair="BTC/USDT", side="LONG", leverage=10,
                tp1=52000.0, stop_loss=48000.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _make_engine(
    prefs_db, *, enabled=True, dry_run=True, allowlist=frozenset({UID}),
    max_per_day=10, balance=1000.0, plan=_SENTINEL, place_result=None,
    place_error=None, verified=True, delegate=True,
):
    cfg = AutotradeConfig(
        enabled=enabled, dry_run=dry_run, network="testnet",
        allowlist=allowlist, default_size_pct=5.0, max_leverage=20,
        max_per_day=max_per_day, min_collateral_usdc=5.0, slippage_bps=100,
    )
    client = AsyncMock()
    client.get_available_usdc = AsyncMock(return_value=balance)
    # Clean account for the risk guard: comfortable value, no open positions,
    # so existing engine-behaviour tests are unaffected by the guard.
    client.get_account_snapshot = AsyncMock(
        return_value=AccountSnapshot(account_value=100_000.0, positions={}),
    )
    client.plan_trade = AsyncMock(
        return_value=_DEFAULT_PLAN if plan is _SENTINEL else plan,
    )
    if place_error is not None:
        client.place_trade = AsyncMock(side_effect=place_error)
    else:
        client.place_trade = AsyncMock(return_value=place_result or _DEFAULT_RESULT)

    delegates = AsyncMock()
    delegates.get = AsyncMock(return_value=(
        SimpleNamespace(trader_address="0xMASTER", is_active=True)
        if delegate else None
    ))
    delegates.get_plaintext_key = AsyncMock(return_value="0xAGENTKEY")
    delegates.mark_trade_success = AsyncMock()
    delegates.mark_trade_failure = AsyncMock()

    verification = AsyncMock()
    verification.get_verified = AsyncMock(return_value=(
        SimpleNamespace(is_active=True) if verified else None
    ))

    send_dm = AsyncMock()
    engine = AutotradeEngine(
        config=cfg, max_collateral_usdc=5000.0, client=client,
        delegates_db=delegates, prefs_db=prefs_db,
        verification_db=verification, send_dm=send_dm,
    )
    return engine, client, delegates, verification, send_dm


async def _opt_in(prefs_db, uid=UID):
    await prefs_db.set_enabled(uid, True)
    await prefs_db.accept_disclosure(uid)


class TestGates:
    @pytest.mark.asyncio
    async def test_disabled_does_nothing(self, prefs_db):
        engine, client, _, _, send_dm = _make_engine(prefs_db, enabled=False)
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal())
        client.plan_trade.assert_not_called()
        send_dm.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_allowlist_does_nothing(self, prefs_db):
        engine, client, _, _, send_dm = _make_engine(prefs_db, allowlist=frozenset())
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal())
        client.plan_trade.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_elite_skipped(self, prefs_db):
        engine, client, _, _, _ = _make_engine(prefs_db, verified=False)
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal())
        client.plan_trade.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_delegate_skipped(self, prefs_db):
        engine, client, _, _, _ = _make_engine(prefs_db, delegate=False)
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal())
        client.plan_trade.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_opted_in_skipped(self, prefs_db):
        engine, client, _, _, _ = _make_engine(prefs_db)
        # enabled but no disclosure -> not ready
        await prefs_db.set_enabled(UID, True)
        await engine.on_new_signal(_signal())
        client.plan_trade.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_direction_skipped(self, prefs_db):
        engine, client, _, _, _ = _make_engine(prefs_db)
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal(side=None))
        client.plan_trade.assert_not_called()


class TestDryRun:
    @pytest.mark.asyncio
    async def test_previews_without_placing(self, prefs_db):
        engine, client, delegates, _, send_dm = _make_engine(prefs_db, dry_run=True)
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal())
        client.place_trade.assert_not_called()
        delegates.mark_trade_success.assert_not_called()
        assert send_dm.await_count == 1
        assert "[DRY RUN]" in send_dm.await_args.args[1]
        # dry-run must not consume a daily slot
        prefs = await prefs_db.get_or_default(UID)
        assert prefs.trades_today == 0

    @pytest.mark.asyncio
    async def test_dedupe_same_signal(self, prefs_db):
        engine, _, _, _, send_dm = _make_engine(prefs_db, dry_run=True)
        await _opt_in(prefs_db)
        sig = _signal(id=42)
        await engine.on_new_signal(sig)
        await engine.on_new_signal(sig)  # re-broadcast
        assert send_dm.await_count == 1  # second claim fails


class TestLive:
    @pytest.mark.asyncio
    async def test_places_and_reports(self, prefs_db):
        engine, client, delegates, _, send_dm = _make_engine(prefs_db, dry_run=False)
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal())
        client.place_trade.assert_awaited_once()
        # sized off balance*pct: 1000 * 5% = $50 collateral requested
        assert client.plan_trade.await_args.kwargs["collateral_usdc"] == pytest.approx(50.0)
        delegates.mark_trade_success.assert_awaited_once()
        assert "filled" in send_dm.await_args.args[1].lower()
        prefs = await prefs_db.get_or_default(UID)
        assert prefs.trades_today == 1

    @pytest.mark.asyncio
    async def test_balance_too_low_skips(self, prefs_db):
        engine, client, _, _, send_dm = _make_engine(
            prefs_db, dry_run=False, balance=50.0,  # 5% -> $2.50 < $5 min
        )
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal())
        client.plan_trade.assert_not_called()
        client.place_trade.assert_not_called()
        assert "too low" in send_dm.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_unlisted_coin_skips(self, prefs_db):
        engine, client, _, _, send_dm = _make_engine(
            prefs_db, dry_run=False, plan=None,  # plan_trade returns None
        )
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal(pair="OBSCURE/USDT"))
        client.place_trade.assert_not_called()
        assert "hyperliquid" in send_dm.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_daily_cap_enforced(self, prefs_db):
        engine, client, _, _, send_dm = _make_engine(
            prefs_db, dry_run=False, max_per_day=1,
        )
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal(id=1))
        await engine.on_new_signal(_signal(id=2))
        assert client.place_trade.await_count == 1
        assert "daily cap" in send_dm.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_place_error_reports_and_releases(self, prefs_db):
        engine, client, delegates, _, send_dm = _make_engine(
            prefs_db, dry_run=False, place_error=HyperliquidError("insufficient margin"),
        )
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal(id=7))
        delegates.mark_trade_failure.assert_awaited_once()
        assert "failed" in send_dm.await_args.args[1].lower()
        # claim released -> the (user, signal) can be retried
        assert await prefs_db.try_claim_fire(UID, 7) is True


class TestManualFire:
    @pytest.mark.asyncio
    async def test_manual_fire_places_for_invoker(self, prefs_db):
        engine, client, delegates, _, send_dm = _make_engine(prefs_db, dry_run=False)
        await _opt_in(prefs_db)
        await engine.manual_fire(UID, pair="XLM/USDT", side="short", leverage=5, stop_loss=0.207)
        client.place_trade.assert_awaited_once()
        delegates.mark_trade_success.assert_awaited_once()
        assert "filled" in send_dm.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_manual_fire_dry_run_previews(self, prefs_db):
        engine, client, _, _, send_dm = _make_engine(prefs_db, dry_run=True)
        await _opt_in(prefs_db)
        await engine.manual_fire(UID, pair="XLM/USDT", side="long", leverage=5)
        client.place_trade.assert_not_called()
        assert "[DRY RUN]" in send_dm.await_args.args[1]

    @pytest.mark.asyncio
    async def test_manual_fire_bad_direction(self, prefs_db):
        engine, client, _, _, send_dm = _make_engine(prefs_db, dry_run=False)
        await _opt_in(prefs_db)
        await engine.manual_fire(UID, pair="XLM/USDT", side="sideways", leverage=5)
        client.place_trade.assert_not_called()
        assert "direction" in send_dm.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_manual_fire_not_allowlisted(self, prefs_db):
        engine, client, _, _, send_dm = _make_engine(prefs_db, allowlist=frozenset({999}), dry_run=False)
        await _opt_in(prefs_db)
        await engine.manual_fire(UID, pair="XLM/USDT", side="short", leverage=5)
        client.place_trade.assert_not_called()
