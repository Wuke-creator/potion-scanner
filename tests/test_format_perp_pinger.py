"""Tests for the structured Perp Pinger renderer."""

from __future__ import annotations

from src.formatter import format_perp_pinger_signal
from src.parser.perp_pinger_parser import PerpPingerSignal


def _signal(**overrides) -> PerpPingerSignal:
    defaults = dict(
        pair="JUP",
        side="SHORT",
        entry=0.235,
        stop_loss=0.247,
        stop_loss_is_conditional=False,
        stop_loss_raw=None,
        take_profit=0.198,
        risk_rr=0.5,
    )
    defaults.update(overrides)
    return PerpPingerSignal(**defaults)


def test_ticker_is_copy_pasteable():
    text = format_perp_pinger_signal(
        _signal(), ref_link="", channel_name="Perp Calls",
        source_type_label="PERPS",
    )
    assert "<code>JUP</code>" in text


def test_entry_is_copy_pasteable():
    text = format_perp_pinger_signal(
        _signal(entry=65000), ref_link="", channel_name="Perp Calls",
        source_type_label="PERPS",
    )
    assert "<code>65000</code>" in text or "<code>65,000</code>" in text


def test_conditional_sl_shows_raw_phrase():
    text = format_perp_pinger_signal(
        _signal(
            stop_loss_is_conditional=True,
            stop_loss_raw="4h candle close above 0.2470$",
        ),
        ref_link="", channel_name="Perp Calls", source_type_label="PERPS",
    )
    assert "candle close above 0.2470" in text


def test_unconditional_sl_no_raw_phrase():
    text = format_perp_pinger_signal(
        _signal(stop_loss_is_conditional=False, stop_loss_raw=None),
        ref_link="", channel_name="Perp Calls", source_type_label="PERPS",
    )
    assert "candle close" not in text


def test_missing_optional_fields_render_clean():
    text = format_perp_pinger_signal(
        _signal(stop_loss=None, take_profit=None, risk_rr=None),
        ref_link="", channel_name="Perp Calls", source_type_label="PERPS",
    )
    # Header still renders.
    assert "<code>JUP</code>" in text
    assert "SHORT" in text
    # Missing fields don't appear at all (no "Stop:" line, no "Target:" line).
    assert "Stop:" not in text
    assert "Target:" not in text


def test_long_uses_green_arrow_emoji():
    text = format_perp_pinger_signal(
        _signal(side="LONG"), ref_link="", channel_name="Perp Calls",
        source_type_label="PERPS",
    )
    # 📈 is the green up arrow used in the rest of the formatter.
    assert "\U0001f4c8" in text


def test_short_uses_red_arrow_emoji():
    text = format_perp_pinger_signal(
        _signal(side="SHORT"), ref_link="", channel_name="Perp Calls",
        source_type_label="PERPS",
    )
    # 📉 is the red down arrow.
    assert "\U0001f4c9" in text


def test_channel_name_appears_in_source_line():
    text = format_perp_pinger_signal(
        _signal(), ref_link="", channel_name="Perp Calls",
        source_type_label="PERPS",
    )
    assert "Perp Calls" in text
