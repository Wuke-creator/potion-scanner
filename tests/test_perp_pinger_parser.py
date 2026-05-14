"""Tests for the Perp Pinger new-call parser."""

from __future__ import annotations

import pytest

from src.parser.perp_pinger_parser import (
    PerpPingerSignal,
    parse_perp_pinger_new_call,
)


JUP_EXAMPLE = """\
New Call Detected
Source: Potion #Perp Calls

SHORT JUP @ 0.2350
Taking another risky short on JUP / Usdt
Entry : 0.2350$
Dca : 0.2420$
Sl : 4h candle close above 0.2470$
Final tp : below 0.1980$
Risk : 0.5RR

Called by

Trade Now: here
"""


def test_jup_example_full_parse():
    s = parse_perp_pinger_new_call(JUP_EXAMPLE)
    assert s is not None
    assert s.pair == "JUP"
    assert s.side == "SHORT"
    assert s.entry == 0.2350
    assert s.stop_loss == 0.2470
    assert s.stop_loss_is_conditional is True
    assert s.stop_loss_raw is not None and "candle close above" in s.stop_loss_raw
    assert s.take_profit == 0.1980
    assert s.risk_rr == 0.5


def test_long_variant():
    msg = """LONG MOODENG @ 0.5630
Entry: 0.5630
Sl: 0.5400
Tp: 0.6100"""
    s = parse_perp_pinger_new_call(msg)
    assert s is not None
    assert s.pair == "MOODENG"
    assert s.side == "LONG"
    assert s.entry == 0.5630
    assert s.stop_loss == 0.5400
    assert s.stop_loss_is_conditional is False
    assert s.take_profit == 0.6100


def test_lowercase_keywords():
    msg = """short btc @ 65000
entry: 65000
sl: 67000
final tp: below 62000"""
    s = parse_perp_pinger_new_call(msg)
    assert s is not None
    assert s.pair == "BTC"
    assert s.side == "SHORT"
    assert s.take_profit == 62000.0


def test_conditional_sl_below():
    msg = """LONG ETH @ 3500
Entry: 3500
Sl: 1h candle close below 3400
Tp: 3700"""
    s = parse_perp_pinger_new_call(msg)
    assert s is not None
    assert s.stop_loss == 3400.0
    assert s.stop_loss_is_conditional is True


def test_missing_tp_does_not_break():
    msg = """SHORT XRP @ 0.50
Entry: 0.50
Sl: 0.55"""
    s = parse_perp_pinger_new_call(msg)
    assert s is not None
    assert s.take_profit is None
    assert s.stop_loss == 0.55


def test_missing_sl_does_not_break():
    msg = """LONG SOL @ 200
Entry: 200
Tp: 220"""
    s = parse_perp_pinger_new_call(msg)
    assert s is not None
    assert s.stop_loss is None
    assert s.take_profit == 220.0


def test_no_header_returns_none():
    assert parse_perp_pinger_new_call("Just some chat") is None
    assert parse_perp_pinger_new_call("") is None
    assert parse_perp_pinger_new_call("Entry: 65000\nSl: 67000") is None


def test_entry_field_overrides_header_at_price():
    # If header has "SHORT X @ 100" but Entry field says 105, prefer 105
    msg = """SHORT FOO @ 100
Entry: 105
Sl: 110"""
    s = parse_perp_pinger_new_call(msg)
    assert s is not None
    assert s.entry == 105.0


def test_header_only_entry_works_when_field_missing():
    msg = "LONG BAR @ 42"
    s = parse_perp_pinger_new_call(msg)
    assert s is not None
    assert s.entry == 42.0


def test_risk_rr_extraction():
    msg = """SHORT XYZ @ 1
Entry: 1
Sl: 2
Tp: 0.5
Risk: 2.5RR"""
    s = parse_perp_pinger_new_call(msg)
    assert s is not None
    assert s.risk_rr == 2.5
