"""Tests for the Perp Pinger update / close / stop / BE / TP-hit parser."""

from __future__ import annotations

from src.parser.perp_pinger_update_parser import (
    LABELS,
    UpdateKind,
    parse_perp_pinger_update,
)


def test_close_with_phrase():
    u = parse_perp_pinger_update("Closing lab here in full closed")
    assert u is not None
    assert u.kind == UpdateKind.CLOSE
    assert u.pair == "LAB"


def test_closed_caps():
    u = parse_perp_pinger_update("CLOSED LAB")
    assert u is not None
    assert u.kind == UpdateKind.CLOSE
    assert u.pair == "LAB"


def test_sold_variant():
    u = parse_perp_pinger_update("Sold JUP at 0.20, taking the bag")
    assert u is not None
    assert u.kind == UpdateKind.CLOSE
    assert u.pair == "JUP"


def test_stopped_out():
    u = parse_perp_pinger_update("Stopped JUP out at 0.2470")
    assert u is not None
    assert u.kind == UpdateKind.STOP
    assert u.pair == "JUP"


def test_tp_hit():
    u = parse_perp_pinger_update("AAVE TP1 hit, took partial")
    assert u is not None
    assert u.kind == UpdateKind.TP_HIT
    assert u.pair == "AAVE"


def test_breakeven():
    u = parse_perp_pinger_update("Moved SL to BE on BTC")
    assert u is not None
    assert u.kind == UpdateKind.BREAKEVEN
    assert u.pair == "BTC"


def test_adjust_catchall():
    u = parse_perp_pinger_update("Moving SL on ETH to 3400")
    assert u is not None
    assert u.kind == UpdateKind.ADJUST
    assert u.pair == "ETH"


def test_no_match_returns_none():
    assert parse_perp_pinger_update("") is None
    assert parse_perp_pinger_update("Just chatting") is None
    assert parse_perp_pinger_update("Random update with no ticker") is None


def test_action_words_not_treated_as_ticker():
    # "STOPPED" itself isn't a ticker even though it's all caps
    u = parse_perp_pinger_update("Stopped out")
    # Either returns None (no ticker found) or extracts a valid one — never
    # returns kind=STOP with pair="STOPPED" or "OUT".
    if u is not None:
        assert u.pair not in ("STOPPED", "OUT", "STOP")


def test_labels_cover_every_kind():
    for kind in UpdateKind:
        assert kind in LABELS
        assert isinstance(LABELS[kind], str) and LABELS[kind]


def test_close_priority_over_adjust():
    # "Closing X and adjusting SL" — CLOSE wins because it's listed first.
    u = parse_perp_pinger_update("Closing BTC and adjusting position size")
    assert u is not None
    assert u.kind == UpdateKind.CLOSE
    assert u.pair == "BTC"
