"""Tests for message type classification against all real signal samples."""

from pathlib import Path

import pytest

from src.parser.classifier import MessageType, classify

SAMPLES_DIR = Path("signals/samples")


def _load(filename: str) -> str:
    return (SAMPLES_DIR / filename).read_text().strip()


# ------------------------------------------------------------------
# Classifier: every sample file → correct MessageType
# ------------------------------------------------------------------

class TestClassifier:
    """Classify all 28 real samples and verify the correct MessageType."""

    # --- SIGNAL_ALERT ---

    def test_signal_alert_01(self):
        assert classify(_load("signal_alert_01.txt")) == MessageType.SIGNAL_ALERT

    def test_signal_alert_04_no_header(self):
        """signal_alert_04 has no 'TRADING SIGNAL ALERT' header — uses fallback."""
        assert classify(_load("signal_alert_04.txt")) == MessageType.SIGNAL_ALERT

    def test_signal_alert_05_kilo_prefix(self):
        assert classify(_load("signal_alert_05.txt")) == MessageType.SIGNAL_ALERT

    def test_signal_alert_06(self):
        assert classify(_load("signal_alert_06.txt")) == MessageType.SIGNAL_ALERT

    # --- TP_HIT ---

    def test_tp_hit_01(self):
        assert classify(_load("tp_hit_01.txt")) == MessageType.TP_HIT

    def test_tp_hit_02(self):
        assert classify(_load("tp_hit_02.txt")) == MessageType.TP_HIT

    def test_tp_hit_03(self):
        assert classify(_load("tp_hit_03.txt")) == MessageType.TP_HIT

    # --- ALL_TP_HIT ---

    def test_all_tp_hit_01(self):
        assert classify(_load("all_tp_hit_01.txt")) == MessageType.ALL_TP_HIT

    def test_all_tp_hit_02(self):
        assert classify(_load("all_tp_hit_02.txt")) == MessageType.ALL_TP_HIT

    def test_all_tp_hit_03(self):
        assert classify(_load("all_tp_hit_03.txt")) == MessageType.ALL_TP_HIT

    # --- BREAKEVEN ---

    def test_breakeven_01(self):
        assert classify(_load("breakeven_01.txt")) == MessageType.BREAKEVEN

    def test_breakeven_02(self):
        assert classify(_load("breakeven_02.txt")) == MessageType.BREAKEVEN

    def test_breakeven_03_tp2(self):
        assert classify(_load("breakeven_03.txt")) == MessageType.BREAKEVEN

    def test_breakeven_04_tp2(self):
        assert classify(_load("breakeven_04.txt")) == MessageType.BREAKEVEN

    # --- STOP_HIT ---

    def test_stop_hit_01(self):
        assert classify(_load("stop_hit_01.txt")) == MessageType.STOP_HIT

    # --- CANCELED ---

    def test_canceled_01(self):
        assert classify(_load("canceled_01.txt")) == MessageType.CANCELED

    def test_canceled_02(self):
        assert classify(_load("canceled_02.txt")) == MessageType.CANCELED

    def test_canceled_03(self):
        assert classify(_load("canceled_03.txt")) == MessageType.CANCELED

    def test_canceled_04(self):
        assert classify(_load("canceled_04.txt")) == MessageType.CANCELED

    def test_canceled_05(self):
        assert classify(_load("canceled_05.txt")) == MessageType.CANCELED

    # --- TRADE_CLOSED ---

    def test_trade_closed_01(self):
        assert classify(_load("trade_closed_01.txt")) == MessageType.TRADE_CLOSED

    def test_trade_closed_02(self):
        assert classify(_load("trade_closed_02.txt")) == MessageType.TRADE_CLOSED

    # --- PREPARATION ---

    def test_preparation_01(self):
        assert classify(_load("preparation_01.txt")) == MessageType.PREPARATION

    def test_preparation_02(self):
        assert classify(_load("preparation_02.txt")) == MessageType.PREPARATION

    def test_preparation_03(self):
        assert classify(_load("preparation_03.txt")) == MessageType.PREPARATION

    def test_preparation_04(self):
        assert classify(_load("preparation_04.txt")) == MessageType.PREPARATION

    # --- MANUAL_UPDATE ---

    def test_manual_update_01(self):
        assert classify(_load("manual_update_01.txt")) == MessageType.MANUAL_UPDATE

    # --- NOISE ---

    def test_noise_01(self):
        assert classify(_load("noise_01.txt")) == MessageType.NOISE


# ------------------------------------------------------------------
# Edge cases
# ------------------------------------------------------------------

class TestClassifierEdgeCases:

    def test_empty_string(self):
        assert classify("") == MessageType.NOISE

    def test_random_text(self):
        assert classify("hello world nothing here") == MessageType.NOISE

    def test_signal_without_header_has_fields(self):
        """A message with ENTRY, SL, and TP fields but no header → SIGNAL_ALERT."""
        msg = "PAIR: BTC/USDT #9999\nENTRY: 50000\nSL: 49000\nTP1: 51000\nTP2: 52000"
        assert classify(msg) == MessageType.SIGNAL_ALERT

    def test_cancel_keyword_variations(self):
        assert classify("Trade #1234 Canceled") == MessageType.CANCELED
        assert classify("CANCEL BTC/USDT #1234") == MessageType.CANCELED

    def test_all_tp_before_single_tp(self):
        """ALL_TP_HIT must match before TP_HIT to avoid false positives."""
        msg = "ALL TAKE-PROFIT TARGETS HIT\nPAIR: BTC/USDT #1234\nPROFIT: 100%"
        assert classify(msg) == MessageType.ALL_TP_HIT
