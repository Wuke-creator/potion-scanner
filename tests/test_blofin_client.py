"""Unit tests for the Blofin client pure helpers (no network).

Covers the two pieces that must be exactly right before any real order
fires: the HMAC signing scheme and the USDC-collateral -> contracts math.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from decimal import Decimal

from src.trading.blofin_client import (
    compute_contracts,
    signal_side_to_blofin,
    sign_request,
)


class TestSignRequest:
    def test_matches_hex_then_base64_scheme(self):
        secret = "topsecret"
        method = "GET"
        path = "/api/v1/asset/balances?accountType=futures"
        ts = "1700000000000"
        nonce = "abc123"
        # Recompute the documented scheme independently.
        prehash = f"{path}{method}{ts}{nonce}"
        expected_hex = hmac.new(
            secret.encode(), prehash.encode(), hashlib.sha256,
        ).hexdigest()
        expected = base64.b64encode(expected_hex.encode()).decode()
        assert sign_request(secret, method, path, ts, nonce) == expected

    def test_sign_is_base64_of_64_char_hex(self):
        sig = sign_request("s", "POST", "/x", "1", "n", '{"a":1}')
        decoded = base64.b64decode(sig).decode()
        assert len(decoded) == 64  # sha256 hex digest length
        int(decoded, 16)  # decodes as hex, else raises

    def test_body_change_changes_signature(self):
        a = sign_request("s", "POST", "/api/v1/trade/order", "1", "n", '{"size":"1"}')
        b = sign_request("s", "POST", "/api/v1/trade/order", "1", "n", '{"size":"2"}')
        assert a != b

    def test_method_is_uppercased(self):
        lower = sign_request("s", "get", "/x", "1", "n")
        upper = sign_request("s", "GET", "/x", "1", "n")
        assert lower == upper


class TestComputeContracts:
    def test_basic_btc_sizing(self):
        # $100 collateral x10 = $1000 notional; BTC @ 50000 -> 0.02 BTC;
        # contractValue 0.001 -> 20 contracts; lot 0.1 divides evenly.
        contracts = compute_contracts(
            collateral_usdc=100, leverage=10, price=50000,
            contract_value=Decimal("0.001"),
            lot_size=Decimal("0.1"), min_size=Decimal("0.1"),
        )
        assert contracts == Decimal("20")

    def test_floors_to_lot_size(self):
        # base_qty 0.0002 / cv 0.001 = 0.2 contracts, lot 0.1 -> 0.2.
        contracts = compute_contracts(
            collateral_usdc=5, leverage=2, price=50000,
            contract_value=Decimal("0.001"),
            lot_size=Decimal("0.1"), min_size=Decimal("0.1"),
        )
        assert contracts == Decimal("0.2")

    def test_below_min_returns_none(self):
        # 1 * 1 / 100000 / 0.001 = 0.01 contracts, floored to lot 0.1 -> 0.
        contracts = compute_contracts(
            collateral_usdc=1, leverage=1, price=100000,
            contract_value=Decimal("0.001"),
            lot_size=Decimal("0.1"), min_size=Decimal("0.1"),
        )
        assert contracts is None

    def test_rounds_down_not_up(self):
        # raw 2.79 contracts must floor to 2.7 at lot 0.1, never 2.8.
        contracts = compute_contracts(
            collateral_usdc=279, leverage=1, price=100,
            contract_value=Decimal("1"),
            lot_size=Decimal("0.1"), min_size=Decimal("0.1"),
        )
        assert contracts == Decimal("2.7")

    def test_integer_lot_size(self):
        # Many alt perps use whole-contract lots. 1234 notional / price 10
        # / cv 1 = 123.4 -> floor to lot 1 -> 123.
        contracts = compute_contracts(
            collateral_usdc=1234, leverage=1, price=10,
            contract_value=Decimal("1"),
            lot_size=Decimal("1"), min_size=Decimal("1"),
        )
        assert contracts == Decimal("123")

    def test_zero_and_negative_inputs_return_none(self):
        assert compute_contracts(
            collateral_usdc=0, leverage=10, price=50000,
            contract_value=Decimal("0.001"),
            lot_size=Decimal("0.1"), min_size=Decimal("0.1"),
        ) is None
        assert compute_contracts(
            collateral_usdc=100, leverage=0, price=50000,
            contract_value=Decimal("0.001"),
            lot_size=Decimal("0.1"), min_size=Decimal("0.1"),
        ) is None
        assert compute_contracts(
            collateral_usdc=100, leverage=10, price=0,
            contract_value=Decimal("0.001"),
            lot_size=Decimal("0.1"), min_size=Decimal("0.1"),
        ) is None


class TestSideMapping:
    def test_long_is_buy(self):
        assert signal_side_to_blofin("LONG") == "buy"
        assert signal_side_to_blofin("long") == "buy"

    def test_short_is_sell(self):
        assert signal_side_to_blofin("SHORT") == "sell"
        assert signal_side_to_blofin("anything_else") == "sell"
