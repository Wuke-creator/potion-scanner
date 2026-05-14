"""Tests for the /connect paste-back parser.

Users paste a free-form message containing their trader address and
delegate key. The parser must tolerate variations in case, separator,
ordering, and surrounding whitespace without ever accepting malformed
hex strings.
"""

from __future__ import annotations

import pytest


def _parse(text: str):
    from src.trading.commands import _parse_paste
    return _parse_paste(text)


def test_exact_format():
    out = _parse(
        "trader: 0xabcdef0123456789abcdef0123456789abcdef01\n"
        "delegate: 0x" + "ab" * 32
    )
    assert out == (
        "0xabcdef0123456789abcdef0123456789abcdef01",
        "0x" + "ab" * 32,
    )


def test_case_insensitive_keys():
    out = _parse(
        "TRADER = 0xABCDEF0123456789abcdef0123456789ABCDEF01\n"
        "DELEGATE = 0x" + "AB" * 32
    )
    assert out is not None
    trader, delegate = out
    assert trader.lower().startswith("0xabcdef")
    assert delegate.lower().startswith("0xab")


def test_delegate_before_trader():
    out = _parse(
        "delegate: 0x" + "ee" * 32 + "\n"
        "trader: 0x" + "11" * 20
    )
    assert out == ("0x" + "11" * 20, "0x" + "ee" * 32)


def test_rejects_missing_trader():
    assert _parse("delegate: 0x" + "ab" * 32) is None


def test_rejects_missing_delegate():
    assert _parse("trader: 0x" + "ab" * 20) is None


def test_rejects_short_trader_address():
    assert _parse(
        "trader: 0xabcd\n"
        "delegate: 0x" + "ab" * 32
    ) is None


def test_rejects_short_delegate_key():
    assert _parse(
        "trader: 0x" + "ab" * 20 + "\n"
        "delegate: 0xdeadbeef"
    ) is None


def test_tolerates_surrounding_chatter():
    out = _parse(
        "Hey, here's my setup:\n"
        "trader: 0x" + "ab" * 20 + "\n"
        "delegate: 0x" + "cd" * 32 + "\n"
        "Thanks!"
    )
    assert out is not None


def test_first_match_wins_when_user_pastes_multiple():
    # If the user pastes twice by accident, we just take the first.
    a = "0x" + "11" * 20
    b = "0x" + "22" * 20
    out = _parse(
        f"trader: {a}\ntrader: {b}\ndelegate: 0x" + "cd" * 32
    )
    assert out is not None
    assert out[0] == a
