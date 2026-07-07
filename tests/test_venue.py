"""Unit tests for the venue adapters (Hyperliquid + Blofin).

The engine is tested against a mock venue elsewhere; here we test the two
real adapters translate correctly to their underlying clients, that the
leverage cap honours the signal, the caller max, and the asset max, and
that venue-native errors surface as VenueError.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.trading.blofin_client import (
    BlofinCreds,
    BlofinError,
    InstrumentInfo,
    OrderResult,
)
from src.trading.hyperliquid_client import (
    AccountSnapshot,
    HyperliquidError,
    TradePlan,
    TradeSubmitResult,
)
from src.trading.venue import (
    BlofinVenue,
    HyperliquidVenue,
    VenueError,
    cap_leverage,
)


class TestCapLeverage:
    def test_signal_leverage_passes_when_under_caps(self):
        assert cap_leverage(16, 75, 20) == 16

    def test_capped_by_caller_max(self):
        assert cap_leverage(50, 75, 20) == 20

    def test_capped_by_asset_max(self):
        assert cap_leverage(50, 10, 20) == 10

    def test_zero_request_uses_ceiling(self):
        assert cap_leverage(0, 75, 20) == 20
        assert cap_leverage(0, 8, 20) == 8

    def test_never_below_one(self):
        assert cap_leverage(0, 0, 0) == 1
        assert cap_leverage(-5, 75, 20) == 20


def _xlm_info():
    return InstrumentInfo(
        inst_id="XLM-USDT", contract_value=Decimal("100"),
        min_size=Decimal("0.1"), lot_size=Decimal("0.1"),
        max_leverage=75, state="live",
    )


def _blofin_venue(*, price=0.2, creds=True):
    client = AsyncMock()
    client.resolve_inst_id = AsyncMock(return_value=_xlm_info())
    client.get_last_price = AsyncMock(return_value=price)
    client.set_leverage = AsyncMock()
    client.place_market_order = AsyncMock(
        return_value=OrderResult(order_id="oid1", raw={"code": "0"}),
    )
    creds_db = AsyncMock()
    creds_db.get = AsyncMock(return_value=(
        SimpleNamespace(is_active=True) if creds else None
    ))
    creds_db.get_creds = AsyncMock(return_value=(
        BlofinCreds(api_key="k", api_secret="s", passphrase="p") if creds else None
    ))
    creds_db.mark_trade_success = AsyncMock()
    creds_db.mark_trade_failure = AsyncMock()
    return BlofinVenue(client, creds_db), client, creds_db


class TestBlofinVenue:
    def test_does_not_support_risk_guard(self):
        v, _, _ = _blofin_venue()
        assert v.supports_risk_guard is False
        assert v.name == "blofin"

    @pytest.mark.asyncio
    async def test_plan_sizes_and_caps_leverage(self):
        v, client, _ = _blofin_venue()
        plan = await v.plan(
            pair="XLM/USDT", direction="SHORT", collateral_usdc=20.0,
            requested_leverage=16, max_leverage=20,
        )
        assert plan is not None
        assert plan.coin == "XLM"
        assert plan.is_long is False
        assert plan.leverage == 16                      # 16 < caps -> passes
        assert plan.notional_usd == pytest.approx(320)  # 20 * 16
        # $320 / $0.2 = 1600 XLM / 100 per contract = 16 contracts
        assert plan.exec_payload["size_contracts"] == Decimal("16")
        assert plan.exec_payload["side"] == "sell"
        assert plan.exec_payload["inst_id"] == "XLM-USDT"
        assert plan.size == pytest.approx(1600.0)

    @pytest.mark.asyncio
    async def test_plan_none_when_unlisted(self):
        v, client, _ = _blofin_venue()
        client.resolve_inst_id = AsyncMock(return_value=None)
        plan = await v.plan(
            pair="NOPE/USDT", direction="LONG", collateral_usdc=20.0,
            requested_leverage=10, max_leverage=20,
        )
        assert plan is None

    @pytest.mark.asyncio
    async def test_plan_none_when_below_min_size(self):
        v, client, _ = _blofin_venue()
        # $1 collateral x1 / $0.2 = 5 XLM / 100 per contract = 0.05 -> below 0.1 min
        plan = await v.plan(
            pair="XLM/USDT", direction="LONG", collateral_usdc=1.0,
            requested_leverage=1, max_leverage=20,
        )
        assert plan is None

    @pytest.mark.asyncio
    async def test_place_sets_leverage_then_orders(self):
        v, client, _ = _blofin_venue()
        plan = await v.plan(
            pair="XLM/USDT", direction="SHORT", collateral_usdc=20.0,
            requested_leverage=16, max_leverage=20,
        )
        res = await v.place(
            uid=1, plan=plan, take_profit=0.19, stop_loss=0.21, slippage_bps=100,
        )
        client.set_leverage.assert_awaited_once()
        assert client.set_leverage.await_args.args[1] == "XLM-USDT"
        assert client.set_leverage.await_args.args[2] == 16
        client.place_market_order.assert_awaited_once()
        kw = client.place_market_order.await_args.kwargs
        assert kw["inst_id"] == "XLM-USDT"
        assert kw["side"] == "sell"
        assert kw["size_contracts"] == Decimal("16")
        assert kw["take_profit"] == 0.19 and kw["stop_loss"] == 0.21
        assert res.ref == "oid1"
        assert res.sl_ok is True and res.tp_ok is True

    @pytest.mark.asyncio
    async def test_place_translates_blofin_error(self):
        v, client, _ = _blofin_venue()
        plan = await v.plan(
            pair="XLM/USDT", direction="LONG", collateral_usdc=20.0,
            requested_leverage=5, max_leverage=20,
        )
        client.place_market_order = AsyncMock(side_effect=BlofinError("rejected", code="1"))
        with pytest.raises(VenueError):
            await v.place(uid=1, plan=plan, take_profit=None, stop_loss=None, slippage_bps=100)

    @pytest.mark.asyncio
    async def test_get_balance_requires_connection(self):
        v, client, _ = _blofin_venue(creds=False)
        with pytest.raises(VenueError):
            await v.get_balance(1)

    @pytest.mark.asyncio
    async def test_snapshot_not_supported(self):
        v, _, _ = _blofin_venue()
        with pytest.raises(NotImplementedError):
            await v.get_account_snapshot(1)


def _hl_venue():
    client = AsyncMock()
    client.get_available_usdc = AsyncMock(return_value=250.0)
    client.plan_trade = AsyncMock(return_value=TradePlan(
        coin="INJ", is_long=True, price=5.0, size=10.0, leverage=5, notional_usd=50.0,
    ))
    client.place_trade = AsyncMock(return_value=TradeSubmitResult(
        coin="INJ", size=10.0, entry_oid=99, entry_avg_px=5.0, sl_ok=True,
        tp_ok=True, raw_entry={},
    ))
    client.get_account_snapshot = AsyncMock(
        return_value=AccountSnapshot(account_value=250.0, positions={}),
    )
    delegates = AsyncMock()
    delegates.get = AsyncMock(return_value=SimpleNamespace(
        trader_address="0xMASTER", is_active=True,
    ))
    delegates.get_plaintext_key = AsyncMock(return_value="0xAGENT")
    delegates.mark_trade_success = AsyncMock()
    delegates.mark_trade_failure = AsyncMock()
    return HyperliquidVenue(client, delegates), client, delegates


class TestHyperliquidVenue:
    def test_supports_risk_guard(self):
        v, _, _ = _hl_venue()
        assert v.supports_risk_guard is True
        assert v.name == "hyperliquid"

    @pytest.mark.asyncio
    async def test_plan_wraps_trade_plan(self):
        v, client, _ = _hl_venue()
        plan = await v.plan(
            pair="INJ/USDT", direction="LONG", collateral_usdc=10.0,
            requested_leverage=5, max_leverage=20,
        )
        assert plan.coin == "INJ" and plan.leverage == 5
        assert plan.exec_payload.coin == "INJ"  # carries the native TradePlan

    @pytest.mark.asyncio
    async def test_place_uses_key_and_master(self):
        v, client, delegates = _hl_venue()
        plan = await v.plan(
            pair="INJ/USDT", direction="LONG", collateral_usdc=10.0,
            requested_leverage=5, max_leverage=20,
        )
        res = await v.place(
            uid=1, plan=plan, take_profit=6.0, stop_loss=4.0, slippage_bps=100,
        )
        kw = client.place_trade.await_args.kwargs
        assert kw["agent_private_key"] == "0xAGENT"
        assert kw["master_address"] == "0xMASTER"
        assert res.ref == "99"

    @pytest.mark.asyncio
    async def test_place_translates_hl_error(self):
        v, client, _ = _hl_venue()
        plan = await v.plan(
            pair="INJ/USDT", direction="LONG", collateral_usdc=10.0,
            requested_leverage=5, max_leverage=20,
        )
        client.place_trade = AsyncMock(side_effect=HyperliquidError("boom"))
        with pytest.raises(VenueError):
            await v.place(uid=1, plan=plan, take_profit=None, stop_loss=None, slippage_bps=100)

    @pytest.mark.asyncio
    async def test_balance_reads_from_master(self):
        v, client, _ = _hl_venue()
        assert await v.get_balance(1) == 250.0
        client.get_available_usdc.assert_awaited_once_with("0xMASTER")
