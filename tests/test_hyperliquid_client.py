"""Unit tests for Hyperliquid client pure helpers + symbol mapping.

No SDK import required: the sizing/price helpers and response parsers are
isolated from the lazy SDK imports, so these run without the SDK installed.
"""

from __future__ import annotations

import pytest

from src.trading.hyperliquid_client import (
    cap_leverage,
    compute_size,
    floor_to,
    round_price,
    _first_avg_px,
    _first_error,
    _first_oid,
)
from src.utils.symbol_mapper import potion_to_hyperliquid


class TestFloorTo:
    def test_floors_not_rounds(self):
        assert floor_to(2.79, 1) == 2.7
        assert floor_to(0.12345, 2) == 0.12
        assert floor_to(20.0, 4) == 20.0

    def test_zero_decimals(self):
        assert floor_to(123.9, 0) == 123.0


class TestRoundPrice:
    def test_five_sig_figs(self):
        assert round_price(50123.4) == 50123.0
        assert round_price(50000) == 50000.0

    def test_small_price_sig_figs(self):
        # 0.0123456 -> 5 sig figs -> 0.012346
        assert round_price(0.0123456) == 0.012346

    def test_zero_price(self):
        assert round_price(0) == 0.0


class TestComputeSize:
    def test_basic(self):
        # $1000 notional @ 50000, 4 szDecimals -> 0.02
        assert compute_size(1000, 50000, 4) == 0.02

    def test_floors_to_sz_decimals(self):
        # 1234/100 = 12.34, szDecimals 1 -> 12.3
        assert compute_size(1234, 100, 1) == 12.3

    def test_below_min_notional_returns_none(self):
        # $5 notional is below the $10 exchange minimum
        assert compute_size(5, 50000, 4) is None

    def test_rounds_to_zero_returns_none(self):
        # 1/100000 = 0.00001, floored at 0 decimals -> 0
        assert compute_size(1, 100000, 0) is None

    def test_nonpositive_inputs(self):
        assert compute_size(0, 50000, 4) is None
        assert compute_size(1000, 0, 4) is None


class TestCapLeverage:
    def test_respects_both_caps(self):
        assert cap_leverage(20, 25, 20) == 20
        assert cap_leverage(50, 25, 20) == 20   # caller cap wins
        assert cap_leverage(50, 10, 20) == 10   # asset cap wins

    def test_default_when_missing(self):
        assert cap_leverage(0, 25, 20) == 5     # falls back to default 5, capped
        assert cap_leverage(3, None, None) == 3

    def test_zero_caps_treated_as_no_cap(self):
        # Zero caps are falsy -> ignored; requested 0 falls back to default 5.
        assert cap_leverage(0, 0, 0) == 5


class TestResponseParsers:
    def test_error_extracted(self):
        r = {"response": {"data": {"statuses": [{"error": "insufficient margin"}]}}}
        assert _first_error(r) == "insufficient margin"

    def test_no_error_on_resting(self):
        r = {"response": {"data": {"statuses": [{"resting": {"oid": 123}}]}}}
        assert _first_error(r) is None
        assert _first_oid(r) == 123

    def test_filled_oid_and_avg_px(self):
        r = {"response": {"data": {"statuses": [{"filled": {"oid": 9, "avgPx": "2501.5"}}]}}}
        assert _first_oid(r) == 9
        assert _first_avg_px(r) == 2501.5

    def test_string_response_is_error(self):
        assert _first_error({"response": "rate limited"}) == "rate limited"


class TestSymbolMapping:
    def test_direct(self):
        assert potion_to_hyperliquid("BTC/USDT") == "BTC"
        assert potion_to_hyperliquid("SOL/USDT") == "SOL"

    def test_kilo_prefix(self):
        assert potion_to_hyperliquid("1000PEPE/USDT") == "kPEPE"
        assert potion_to_hyperliquid("1000BONK/USDT") == "kBONK"

    def test_overrides(self):
        assert potion_to_hyperliquid("MATIC/USDT") == "POL"
        assert potion_to_hyperliquid("PEPE/USDT") == "kPEPE"

    def test_validation_against_meta_raises_when_unlisted(self):
        meta = {"BTC": {}, "ETH": {}}
        with pytest.raises(ValueError):
            potion_to_hyperliquid("DOGE/USDT", available_coins=meta)

    def test_validation_passes_when_listed(self):
        meta = {"BTC": {}, "ETH": {}}
        assert potion_to_hyperliquid("BTC/USDT", available_coins=meta) == "BTC"
