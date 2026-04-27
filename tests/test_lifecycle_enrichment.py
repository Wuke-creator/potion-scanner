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
    # Leverage rendered with code tags for tap-to-copy
    assert "<code>50</code>x" in out
    # Entry / SL / TPs all wrapped in <code> for tap-to-copy
    assert "<code>0.099</code>" in out
    assert "<code>0.105</code>" in out
    assert "<code>0.094</code>" in out  # TP1
    # Per-pair Ostium deeplink populated for Trade Now (WET base)
    assert "app.ostium.com/trade?from=WET&amp;to=USD" in out


def test_lifecycle_event_ostium_deeplink_fallback_to_caption_pair():
    """Even without an original_signal match, the Ostium deeplink should
    use a ticker pulled from the caption (e.g. 'WET Update: TP1 here')."""
    out = format_lifecycle_event(
        label="Take Profit Hit",
        raw_message="WET Update: TP1 here, move SL to BE",
        ref_link="https://app.ostium.com/?ref=PTION",
        channel_name="Pingu Charts",
        source_type_label="Perps",
        original_signal=None,
    )
    assert "app.ostium.com/trade?from=WET&amp;to=USD" in out


def test_lifecycle_event_blofin_ref_link_unchanged():
    """Non-Ostium ref links pass through unchanged (no per-pair rewrite)."""
    out = format_lifecycle_event(
        label="Take Profit Hit",
        raw_message="WET Update: TP1 here",
        ref_link="https://partner.blofin.com/d/potion",
        channel_name="Mac's Calls",
        source_type_label="Perps",
        original_signal=_make_open_signal(),
    )
    # Bare Blofin URL preserved (no per-pair query string added)
    assert "partner.blofin.com/d/potion" in out
    assert "from=WET" not in out


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
    # Entry rendered with code tags for tap-to-copy
    assert "<code>3,000</code>" in out or "<code>3000</code>" in out
    assert "<code>3,100</code>" in out or "<code>3100</code>" in out
    # Missing fields should be absent (not rendered as None)
    assert "None" not in out
    assert "Stop:" not in out
    assert "TP2:" not in out
    assert "TP3:" not in out
