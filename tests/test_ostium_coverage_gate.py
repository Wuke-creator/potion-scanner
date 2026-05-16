"""Tests for the Ostium coverage gate.

Covers three layers:
  - formatter: deeplink falls back to bare ref link when the token is
    known NOT on Ostium; deeplink preserved when supported/unknown.
  - formatter: 1-Tap button only rendered when caller passes a signal id.
  - executor_client: symbol cache returns None until first successful
    fetch (callers fail safe), and is_symbol_supported reflects the set.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.formatter import (
    QUICK_TRADE_CALLBACK_PREFIX,
    _resolve_trade_url,
    build_signal_keyboard,
)

_OSTIUM_REF = "https://app.ostium.com/?ref=PTION"
_BLOFIN_REF = "https://partner.blofin.com/d/potion"


# ---- _resolve_trade_url ---------------------------------------------------

def test_ostium_deeplink_when_supported():
    url = _resolve_trade_url(_OSTIUM_REF, "BTC/USD", True)
    assert "ostium.com/trade?" in url
    assert "from=BTC" in url


def test_ostium_deeplink_when_unknown():
    # None = coverage unknown -> keep the deeplink (common case is listed).
    url = _resolve_trade_url(_OSTIUM_REF, "BTC/USD", None)
    assert "ostium.com/trade?" in url
    assert "from=BTC" in url


def test_ostium_falls_back_when_not_supported():
    # Known NOT on Ostium -> bare ref link, never a dead /trade?from=X.
    url = _resolve_trade_url(_OSTIUM_REF, "FARTCOIN/USD", False)
    assert url == _OSTIUM_REF
    assert "/trade?" not in url


def test_blofin_unaffected_by_ostium_flag():
    url = _resolve_trade_url(_BLOFIN_REF, "DOGE/USDT", False)
    assert "blofin.com/futures/DOGE-USDT" in url


def test_empty_ref_link_passes_through():
    assert _resolve_trade_url("", "BTC/USD", False) == ""


# ---- build_signal_keyboard ------------------------------------------------

def _trade_now_url(kb) -> str:
    for row in kb.inline_keyboard:
        for btn in row:
            if btn.url and ("ostium" in btn.url or "blofin" in btn.url):
                return btn.url
    return ""


def _has_one_tap(kb) -> bool:
    return any(
        (btn.callback_data or "").startswith(QUICK_TRADE_CALLBACK_PREFIX)
        for row in kb.inline_keyboard
        for btn in row
    )


def test_keyboard_one_tap_present_when_id_given():
    kb = build_signal_keyboard(
        _OSTIUM_REF, "BTC/USD", quick_trade_signal_id=42, ostium_supported=True,
    )
    assert _has_one_tap(kb)
    assert "from=BTC" in _trade_now_url(kb)


def test_keyboard_no_one_tap_when_id_none():
    kb = build_signal_keyboard(
        _OSTIUM_REF, "BTC/USD", quick_trade_signal_id=None, ostium_supported=True,
    )
    assert not _has_one_tap(kb)


def test_keyboard_deeplink_fallback_when_unsupported():
    kb = build_signal_keyboard(
        _OSTIUM_REF, "FARTCOIN/USD",
        quick_trade_signal_id=None, ostium_supported=False,
    )
    # Token not on Ostium -> bare ref link, no dead deeplink.
    assert _trade_now_url(kb) == _OSTIUM_REF


# ---- TradeExecutorClient symbol cache -------------------------------------

class _AsyncCM:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *a):
        return False


def _client_with_pairs_response(payload: dict):
    from src.trading.executor_client import TradeExecutorClient

    c = TradeExecutorClient(
        base_url="http://executor.internal:3001", shared_secret="x",
    )
    resp = MagicMock()
    resp.json = AsyncMock(return_value=payload)
    session = MagicMock()
    session.get = MagicMock(return_value=_AsyncCM(resp))
    c._session = session
    return c


@pytest.mark.asyncio
async def test_symbols_none_before_open():
    from src.trading.executor_client import TradeExecutorClient

    c = TradeExecutorClient(base_url="http://x", shared_secret="x")
    # Never opened -> session None -> returns the (None) cache.
    assert await c.get_supported_symbols() is None
    # is_symbol_supported must surface None (caller fails safe).
    assert await c.is_symbol_supported("BTC") is None


@pytest.mark.asyncio
async def test_symbols_populated_from_pairs_endpoint():
    c = _client_with_pairs_response({"symbols": ["BTC", "ETH", "SOL"], "count": 3})
    syms = await c.get_supported_symbols()
    assert syms == {"BTC", "ETH", "SOL"}
    assert await c.is_symbol_supported("btc") is True
    assert await c.is_symbol_supported("FARTCOIN") is False


@pytest.mark.asyncio
async def test_empty_pairs_response_keeps_cache_none():
    # Executor's own fetch failed -> returns [] -> we must NOT treat that
    # as "nothing supported"; keep cache None so callers fail safe.
    c = _client_with_pairs_response({"symbols": [], "count": 0, "error": "rpc down"})
    assert await c.get_supported_symbols() is None
    assert await c.is_symbol_supported("BTC") is None


@pytest.mark.asyncio
async def test_cache_not_overwritten_by_later_empty():
    from src.trading.executor_client import TradeExecutorClient

    c = TradeExecutorClient(base_url="http://x", shared_secret="x")
    # First call: good list.
    resp_good = MagicMock()
    resp_good.json = AsyncMock(return_value={"symbols": ["BTC"], "count": 1})
    # Second call: empty (executor blip).
    resp_bad = MagicMock()
    resp_bad.json = AsyncMock(return_value={"symbols": [], "count": 0})
    session = MagicMock()
    session.get = MagicMock(side_effect=[_AsyncCM(resp_good), _AsyncCM(resp_bad)])
    c._session = session

    first = await c.get_supported_symbols()
    assert first == {"BTC"}
    # Force TTL expiry so the next call refetches.
    c._symbols_fetched_at = 0.0
    second = await c.get_supported_symbols()
    # Blip returned [] -> keep the last good set, don't wipe it.
    assert second == {"BTC"}
