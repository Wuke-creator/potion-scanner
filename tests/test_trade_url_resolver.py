"""Tests for the per-pair Trade-now URL resolver.

Covers the shared ``_resolve_trade_url`` plus the Ostium and Blofin
builders, plus the integration via ``build_signal_keyboard`` so the
inline Trade-now button on every alert gets the right URL.
"""
from __future__ import annotations

from src.formatter import (
    _build_blofin_trade_url,
    _build_ostium_trade_url,
    _resolve_trade_url,
    build_signal_keyboard,
)


# ---------------------------------------------------------------------------
# Ostium per-pair deeplink
# ---------------------------------------------------------------------------

def test_ostium_deeplink_uses_pair_base_and_preserves_ref():
    out = _build_ostium_trade_url(
        "https://app.ostium.com/?ref=PTION", "BTC/USDT",
    )
    assert "from=BTC" in out
    assert "to=USD" in out
    assert "ref=PTION" in out
    assert out.startswith("https://app.ostium.com/trade?")


def test_ostium_deeplink_falls_back_when_no_base():
    bare = "https://app.ostium.com/?ref=PTION"
    assert _build_ostium_trade_url(bare, "") == bare


def test_ostium_deeplink_handles_pair_with_leverage_suffix():
    out = _build_ostium_trade_url(
        "https://app.ostium.com/?ref=PTION", "ETH/USD 10x",
    )
    assert "from=ETH" in out


# ---------------------------------------------------------------------------
# Blofin per-pair deeplink
# ---------------------------------------------------------------------------

def test_blofin_deeplink_extracts_invitecode_from_path():
    out = _build_blofin_trade_url(
        "https://partner.blofin.com/d/potion", "BTC/USDT",
    )
    assert "blofin.com/futures/BTC-USDT" in out
    assert "invitecode=potion" in out


def test_blofin_deeplink_uppercases_base():
    out = _build_blofin_trade_url(
        "https://partner.blofin.com/d/potion", "wet/usdt",
    )
    assert "BTC" not in out  # sanity
    assert "futures/WET-USDT" in out


def test_blofin_deeplink_extracts_invitecode_from_query():
    """Some Blofin partner URLs use ?invitecode=<code> instead of /d/<code>."""
    out = _build_blofin_trade_url(
        "https://blofin.com/?invitecode=somecode", "ETH/USDT",
    )
    assert "invitecode=somecode" in out


def test_blofin_deeplink_falls_back_when_no_base():
    bare = "https://partner.blofin.com/d/potion"
    assert _build_blofin_trade_url(bare, "") == bare


# ---------------------------------------------------------------------------
# _resolve_trade_url dispatch
# ---------------------------------------------------------------------------

def test_resolve_dispatches_ostium():
    out = _resolve_trade_url(
        "https://app.ostium.com/?ref=PTION", "BTC/USDT",
    )
    assert "app.ostium.com/trade" in out
    assert "from=BTC" in out


def test_resolve_dispatches_blofin():
    out = _resolve_trade_url(
        "https://partner.blofin.com/d/potion", "ETH/USDT",
    )
    assert "blofin.com/futures/ETH-USDT" in out


def test_resolve_passthrough_for_unknown_exchange():
    bare = "https://trade.padre.gg/rk/orangie"
    assert _resolve_trade_url(bare, "WIF/USDT") == bare


def test_resolve_empty_ref_link_passthrough():
    assert _resolve_trade_url("", "BTC/USDT") == ""


# ---------------------------------------------------------------------------
# Inline keyboard integration
# ---------------------------------------------------------------------------

def test_keyboard_uses_blofin_deeplink_for_blofin_channel():
    kb = build_signal_keyboard(
        ref_link="https://partner.blofin.com/d/potion", pair="ETH/USDT",
    )
    trade_btn = kb.inline_keyboard[0][0]
    assert "blofin.com/futures/ETH-USDT" in trade_btn.url
    assert "invitecode=potion" in trade_btn.url


def test_keyboard_uses_ostium_deeplink_for_ostium_channel():
    kb = build_signal_keyboard(
        ref_link="https://app.ostium.com/?ref=PTION", pair="BTC/USDT",
    )
    trade_btn = kb.inline_keyboard[0][0]
    assert "app.ostium.com/trade" in trade_btn.url
    assert "from=BTC" in trade_btn.url


def test_keyboard_passes_through_unknown_ref_link():
    kb = build_signal_keyboard(
        ref_link="https://trade.padre.gg/rk/orangie", pair="WIF/USDT",
    )
    trade_btn = kb.inline_keyboard[0][0]
    assert trade_btn.url == "https://trade.padre.gg/rk/orangie"
