"""Tests for lifecycle event enrichment via the open_signals memory layer.

Verifies that ``format_lifecycle_event`` renders an "From the original
call" block when an OpenSignal is supplied, and skips it otherwise.
"""
from __future__ import annotations

from src.automations.open_signals_db import OpenSignal
from src.formatter import format_lifecycle_event


def _make_open_signal() -> OpenSignal:
    return OpenSignal(
        channel_id=42,
        pair="WET/USDT",
        normalised_base="WET",
        side="SHORT",
        leverage=50,
        entry=0.099,
        stop_loss=0.105,
        tp1=0.094,
        tp2=0.090,
        tp3=0.085,
        trade_id=None,
        status="open",
        opened_at=0,
        last_event_at=0,
        raw_message="",
    )


def test_lifecycle_event_without_original_signal_omits_block():
    out = format_lifecycle_event(
        label="Take Profit Hit",
        raw_message="WET Update: TP1 here, move SL to BE",
        ref_link="https://app.ostium.com/?ref=PTION",
        channel_name="Pingu Charts",
        source_type_label="Perps",
    )
    assert "Trade Update: Take Profit Hit" in out
    assert "WET Update: TP1 here" in out
    assert "From the original call" not in out


def test_lifecycle_event_with_original_signal_includes_block():
    out = format_lifecycle_event(
        label="Take Profit Hit",
        raw_message="WET Update: TP1 here, move SL to BE",
        ref_link="https://app.ostium.com/?ref=PTION",
        channel_name="Pingu Charts",
        source_type_label="Perps",
        original_signal=_make_open_signal(),
    )
    # Update body still present
    assert "WET Update: TP1 here" in out
    # Enrichment block rendered with original numbers
    assert "From the original call" in out
    assert "WET/USDT" in out
    assert "SHORT" in out
    assert "50x" in out
    assert "Entry: 0.099" in out
    # Stop loss is rendered as "0.105"
    assert "SL: 0.105" in out
    # TP1 rendered (the price the user is being told to take profit at)
    assert "TP1: 0.094" in out


def test_lifecycle_block_handles_partial_signal():
    """OCR-recorded signals may miss SL or one of the TPs. The block
    should render only the populated fields, not 'None'."""
    partial = OpenSignal(
        channel_id=42,
        pair="ETH/USDT",
        normalised_base="ETH",
        side="LONG",
        leverage=10,
        entry=3000.0,
        stop_loss=None,
        tp1=3100.0,
        tp2=None,
        tp3=None,
        trade_id=None,
        status="open",
        opened_at=0,
        last_event_at=0,
        raw_message="",
    )
    out = format_lifecycle_event(
        label="Take Profit Hit",
        raw_message="ETH Update: TP1 here",
        ref_link="https://example.com/ref",
        channel_name="Test Channel",
        source_type_label="Perps",
        original_signal=partial,
    )
    assert "From the original call" in out
    assert "Entry: 3,000" in out or "Entry: 3000" in out
    assert "TP1: 3,100" in out or "TP1: 3100" in out
    # Missing fields should be absent (not rendered as None)
    assert "None" not in out
    assert "SL:" not in out
    assert "TP2:" not in out
    assert "TP3:" not in out
