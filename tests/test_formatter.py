"""Tests for src/formatter.py — formatting functions are pure, easy to verify."""

from pathlib import Path

from src.formatter import (
    _wrap_prices_in_code,
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

    def test_perp_pinger_signal_has_tap_to_copy_prices(self):
        """The Perp Pinger format always falls back to format_unknown_message
        because parse_signal can't parse it. Subscribers need to copy
        Entry / SL / TP into their exchange — those numbers MUST be
        wrapped in <code> for tap-to-copy on Telegram."""
        body = (
            "LONG FARTCOIN @ 0.1945 | TP1: 0.2051\n"
            "FARTCOIN LIMIT LONG\n\n"
            "Entry: 0.1945\n"
            "SL: 0.1819\n"
            "Tp1: 0.2051\n"
            "Full TP: 0.2322"
        )
        text = format_unknown_message(
            raw_message=body,
            ref_link="https://app.ostium.com/?ref=PTION",
            channel_name="Perp Calls",
            source_type_label="PERPS",
        )
        # Each of Entry / SL / TP1 / Full TP gets a tap-to-copy wrapper.
        assert "<code>0.1945</code>" in text
        assert "<code>0.1819</code>" in text
        assert "<code>0.2051</code>" in text
        assert "<code>0.2322</code>" in text
        # The "@ 0.1945" inline shorthand on the header line is also wrapped.
        assert "@ <code>0.1945</code>" in text or "@<code>0.1945</code>" in text


class TestWrapPricesInCode:
    def test_wraps_entry_label(self):
        assert "<code>0.1945</code>" in _wrap_prices_in_code("Entry: 0.1945")

    def test_wraps_sl_label(self):
        assert "<code>0.1819</code>" in _wrap_prices_in_code("SL: 0.1819")

    def test_wraps_tp_with_digit(self):
        out = _wrap_prices_in_code("TP1: 0.2051\nTP2: 0.21\nTP3: 0.22")
        assert "<code>0.2051</code>" in out
        assert "<code>0.21</code>" in out
        assert "<code>0.22</code>" in out

    def test_wraps_full_tp(self):
        assert "<code>0.2322</code>" in _wrap_prices_in_code("Full TP: 0.2322")

    def test_wraps_at_inline_shorthand(self):
        out = _wrap_prices_in_code("LONG FARTCOIN @ 0.1945")
        assert "@ <code>0.1945</code>" in out

    def test_label_is_case_insensitive(self):
        assert "<code>0.5</code>" in _wrap_prices_in_code("entry: 0.5")
        assert "<code>0.5</code>" in _wrap_prices_in_code("Tp1: 0.5")
        assert "<code>0.5</code>" in _wrap_prices_in_code("sl: 0.5")

    def test_label_without_value_unchanged(self):
        # "FARTCOIN LIMIT LONG" has no colon-and-number — must not match.
        assert _wrap_prices_in_code("FARTCOIN LIMIT LONG") == "FARTCOIN LIMIT LONG"

    def test_handles_decimal_and_integer(self):
        assert "<code>3000</code>" in _wrap_prices_in_code("Entry: 3000")
        assert "<code>3,000</code>" in _wrap_prices_in_code("Entry: 3,000")

    def test_idempotent_on_already_wrapped_text(self):
        already = "Entry: <code>0.1945</code>"
        # Running the wrapper again should not produce <code><code>...
        out = _wrap_prices_in_code(already)
        assert "<code><code>" not in out
        assert "<code>0.1945</code>" in out

    def test_multiple_labels_all_wrapped(self):
        body = "Entry: 1.0 SL: 0.9 TP1: 1.1 TP2: 1.2"
        out = _wrap_prices_in_code(body)
        assert out.count("<code>") == 4

    def test_does_not_wrap_random_numbers(self):
        # Timestamp digits and channel names with numbers must not get
        # wrapped — only label-prefixed prices.
        out = _wrap_prices_in_code("Posted at 19:50 UTC in #channel-2")
        assert "<code>" not in out


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
