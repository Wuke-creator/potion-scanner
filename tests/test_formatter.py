"""Tests for src/formatter.py — formatting functions are pure, easy to verify."""

from pathlib import Path

from src.formatter import (
    format_lifecycle_event,
    format_parsed_signal,
    format_unknown_message,
    label_for_source_type,
)
from src.parser import parse_signal

SAMPLES_DIR = Path("signals/samples")


def _load(filename: str) -> str:
    return (SAMPLES_DIR / filename).read_text(encoding="utf-8").strip()


class TestLabelForSourceType:
    def test_perps_label(self):
        assert label_for_source_type("perps") == "PERPS"

    def test_memecoin_label(self):
        assert label_for_source_type("memecoin") == "MEMECOIN"

    def test_unknown_label_uppercased(self):
        assert label_for_source_type("foo") == "FOO"


class TestFormatParsedSignal:
    def test_signal_alert_01_includes_all_fields(self):
        signal = parse_signal(_load("signal_alert_01.txt"))
        text = format_parsed_signal(
            signal=signal,
            ref_link="https://partner.blofin.com/d/potion",
            channel_name="Perp Bot Calls",
            source_type_label="PERPS",
        )
        assert "ZK/USDT" in text
        assert "SHORT" in text
        assert "0.02153" in text
        assert "0.02236" in text
        assert "14" in text  # leverage value (now wrapped in <code> tags)
        assert "MEDIUM" in text
        assert "Perp Bot Calls" in text

    def test_source_appears_near_top(self):
        signal = parse_signal(_load("signal_alert_01.txt"))
        text = format_parsed_signal(
            signal=signal,
            ref_link="https://partner.blofin.com/d/potion",
            channel_name="Perp Bot Calls",
            source_type_label="PERPS",
        )
        lines = text.split("\n")
        source_line = [i for i, l in enumerate(lines) if "Source:" in l]
        assert source_line and source_line[0] <= 2

    def test_no_type_field_in_output(self):
        signal = parse_signal(_load("signal_alert_01.txt"))
        text = format_parsed_signal(
            signal=signal,
            ref_link="https://example.com",
            channel_name="Test",
            source_type_label="PERPS",
        )
        assert "<b>Type:</b>" not in text

    def test_ref_link_not_in_text_body(self):
        """Ref link is in the keyboard buttons, not inline text."""
        signal = parse_signal(_load("signal_alert_01.txt"))
        text = format_parsed_signal(
            signal=signal,
            ref_link="https://partner.blofin.com/d/potion",
            channel_name="Perp Bot Calls",
            source_type_label="PERPS",
        )
        assert "https://partner.blofin.com" not in text

    def test_channel_name_present(self):
        signal = parse_signal(_load("signal_alert_01.txt"))
        text = format_parsed_signal(
            signal=signal,
            ref_link="https://trade.padre.gg/rk/orangie",
            channel_name="Prediction Calls",
            source_type_label="MEMECOIN",
        )
        assert "Prediction Calls" in text


class TestFormatLifecycleEvent:
    def test_tp_hit_includes_label_and_link(self):
        text = format_lifecycle_event(
            label="Take Profit Hit",
            raw_message=_load("tp_hit_01.txt"),
            ref_link="https://partner.blofin.com/d/potion",
            channel_name="Perp Bot Calls",
            source_type_label="PERPS",
        )
        assert "Take Profit Hit" in text
        assert '<a href="https://partner.blofin.com/d/potion">here</a>' in text
        assert "Perp Bot Calls" in text

    def test_truncates_huge_messages(self):
        huge = "A" * 5000
        text = format_lifecycle_event(
            label="Manual Update",
            raw_message=huge,
            ref_link="https://example.com",
            channel_name="Test",
            source_type_label="PERPS",
        )
        assert len(text) < 4096
        assert "..." in text


class TestFormatUnknownMessage:
    def test_forwards_raw_text_with_link(self):
        text = format_unknown_message(
            raw_message="bullish on $PEPE this week, dca below 0.000005",
            ref_link="https://trade.padre.gg/rk/orangie",
            channel_name="Prediction Calls",
            source_type_label="MEMECOIN",
        )
        assert "bullish on $PEPE" in text
        assert '<a href="https://trade.padre.gg/rk/orangie">here</a>' in text
        assert "Prediction Calls" in text

    def test_truncates_huge_messages(self):
        huge = "B" * 5000
        text = format_unknown_message(
            raw_message=huge,
            ref_link="https://example.com",
            channel_name="Test",
            source_type_label="MEMECOIN",
        )
        assert len(text) < 4096
        assert "..." in text


class TestHtmlEscaping:
    def test_special_chars_in_channel_name(self):
        signal = parse_signal(_load("signal_alert_01.txt"))
        text = format_parsed_signal(
            signal=signal,
            ref_link="https://example.com",
            channel_name="test<script>",
            source_type_label="PERPS",
        )
        assert "test&lt;script&gt;" in text
        assert "<script>" not in text
