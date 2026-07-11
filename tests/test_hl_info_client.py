"""Tests for the Hyperliquid public-data client parsers.

No network: the parsers are pure and the client methods are exercised by
stubbing the low-level _get_json/_post_info transport.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.trading.hl_info_client import (
    HyperliquidInfoClient,
    LeaderboardRow,
    parse_clearinghouse_state,
    parse_leaderboard_row,
)


def _lb_row(**kw):
    base = {
        "ethAddress": "0xAbC0000000000000000000000000000000000001",
        "accountValue": "125000.5",
        "windowPerformances": [
            ["day", {"pnl": "150.0", "roi": "0.01", "vlm": "20000"}],
            ["week", {"pnl": "900.0", "roi": "0.05", "vlm": "90000"}],
            ["month", {"pnl": "4000.0", "roi": "0.2", "vlm": "400000"}],
            ["allTime", {"pnl": "60000.0", "roi": "1.5", "vlm": "2000000"}],
        ],
        "displayName": "whale",
    }
    base.update(kw)
    return base


class TestParseLeaderboardRow:
    def test_flattens_windows(self):
        row = parse_leaderboard_row(_lb_row())
        assert row is not None
        assert row.address.startswith("0xAbC")
        assert row.account_value == 125000.5
        assert row.pnl["month"] == 4000.0
        assert row.roi["allTime"] == 1.5
        assert row.volume["week"] == 90000.0
        assert row.display_name == "whale"

    def test_accepts_dict_window_form(self):
        raw = _lb_row(windowPerformances={
            "day": {"pnl": "1", "roi": "0.1", "vlm": "10"},
        })
        row = parse_leaderboard_row(raw)
        assert row.pnl == {"day": 1.0}

    def test_no_address_returns_none(self):
        assert parse_leaderboard_row({"accountValue": "5"}) is None

    def test_garbage_numbers_default_to_zero(self):
        raw = _lb_row(accountValue="not-a-number")
        row = parse_leaderboard_row(raw)
        assert row.account_value == 0.0


class TestParseClearinghouseState:
    def test_parses_positions_and_account_value(self):
        state = parse_clearinghouse_state({
            "marginSummary": {"accountValue": "50000"},
            "assetPositions": [
                {"type": "oneWay", "position": {
                    "coin": "HYPE", "szi": "100.5", "entryPx": "25.0",
                    "leverage": {"type": "cross", "value": 10},
                    "positionValue": "2512.5", "marginUsed": "251.2",
                }},
                {"type": "oneWay", "position": {
                    "coin": "ZEC", "szi": "-40", "entryPx": "300",
                    "leverage": {"type": "isolated", "value": 5},
                    "positionValue": "12000", "marginUsed": "2400",
                }},
            ],
        })
        assert state.account_value == 50000.0
        assert set(state.positions) == {"HYPE", "ZEC"}
        hype = state.positions["HYPE"]
        assert hype.is_long and hype.leverage == 10 and hype.margin_used == 251.2
        zec = state.positions["ZEC"]
        assert not zec.is_long and zec.szi == -40.0 and zec.notional == 12000.0

    def test_zero_size_positions_dropped(self):
        state = parse_clearinghouse_state({
            "marginSummary": {"accountValue": "1"},
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": "0.0"}},
            ],
        })
        assert state.positions == {}

    def test_empty_body(self):
        state = parse_clearinghouse_state({})
        assert state.account_value == 0.0
        assert state.positions == {}


class TestClientMethods:
    @pytest.mark.asyncio
    async def test_get_leaderboard_unwraps_rows(self):
        client = HyperliquidInfoClient()
        client._get_json = AsyncMock(return_value={
            "leaderboardRows": [_lb_row(), {"junk": True}, "not-a-dict"],
        })
        rows = await client.get_leaderboard()
        assert len(rows) == 1
        assert isinstance(rows[0], LeaderboardRow)

    @pytest.mark.asyncio
    async def test_get_user_fills_non_list_becomes_empty(self):
        client = HyperliquidInfoClient()
        client._post_info = AsyncMock(return_value={"error": "rate limited"})
        assert await client.get_user_fills("0x1") == []

    @pytest.mark.asyncio
    async def test_get_candles_passes_request_shape(self):
        client = HyperliquidInfoClient()
        client._post_info = AsyncMock(return_value=[{"t": 1}])
        out = await client.get_candles(
            "HYPE", interval="1h", start_ms=1000, end_ms=2000,
        )
        assert out == [{"t": 1}]
        payload = client._post_info.call_args.args[0]
        assert payload["type"] == "candleSnapshot"
        assert payload["req"] == {
            "coin": "HYPE", "interval": "1h",
            "startTime": 1000, "endTime": 2000,
        }

    @pytest.mark.asyncio
    async def test_clearinghouse_state_parses(self):
        client = HyperliquidInfoClient()
        client._post_info = AsyncMock(return_value={
            "marginSummary": {"accountValue": "77"},
            "assetPositions": [],
        })
        state = await client.get_clearinghouse_state("0x1")
        assert state.account_value == 77.0
