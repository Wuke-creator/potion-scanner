"""Tests for build_signal_keyboard with the new quick_trade_signal_id arg.

When the id is provided, the keyboard should grow a second row containing
a 1-Tap Trade callback button. When None, the legacy two-button layout
must be preserved bit-for-bit so existing Trade-now flows are untouched.
"""

from __future__ import annotations

from src.formatter import QUICK_TRADE_CALLBACK_PREFIX, build_signal_keyboard


def test_no_quick_trade_id_preserves_legacy_layout():
    kb = build_signal_keyboard(
        ref_link="https://app.ostium.com/?ref=PTION",
        pair="BTC/USD",
    )
    rows = kb.inline_keyboard
    assert len(rows) == 1
    assert len(rows[0]) == 2
    assert rows[0][0].text.endswith("Trade now")
    assert rows[0][1].text.endswith("Chart")
    assert rows[0][0].url is not None
    assert rows[0][1].url is not None


def test_quick_trade_id_prepends_callback_row():
    kb = build_signal_keyboard(
        ref_link="https://app.ostium.com/?ref=PTION",
        pair="ETH/USD",
        quick_trade_signal_id=42,
    )
    rows = kb.inline_keyboard
    assert len(rows) == 2
    assert "1-Tap" in rows[0][0].text
    assert rows[0][0].callback_data == f"{QUICK_TRADE_CALLBACK_PREFIX}42"
    assert rows[0][0].url is None
    assert rows[1][0].text.endswith("Trade now")
    assert rows[1][1].text.endswith("Chart")


def test_ostium_url_rewritten_to_per_pair_deeplink():
    kb = build_signal_keyboard(
        ref_link="https://app.ostium.com/?ref=PTION",
        pair="SOL/USDT",
    )
    trade_button = kb.inline_keyboard[0][0]
    assert "from=SOL" in (trade_button.url or "")
    assert "to=USD" in (trade_button.url or "")
    assert "ref=PTION" in (trade_button.url or "")


def test_blofin_url_still_works():
    kb = build_signal_keyboard(
        ref_link="https://partner.blofin.com/d/potion",
        pair="DOGE/USDT",
    )
    trade_button = kb.inline_keyboard[0][0]
    assert "blofin.com/futures/DOGE-USDT" in (trade_button.url or "")
