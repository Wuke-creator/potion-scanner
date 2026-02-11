"""Tests for update parsers — TP hits, SL, breakeven, cancel, etc."""

from pathlib import Path

import pytest

from src.parser.update_parser import (
    UpdateParseError,
    parse_all_tp_hit,
    parse_breakeven,
    parse_canceled,
    parse_manual_update,
    parse_preparation,
    parse_sl_update,
    parse_stop_hit,
    parse_tp_hit,
    parse_trade_closed,
)

SAMPLES_DIR = Path("signals/samples")


def _load(filename: str) -> str:
    return (SAMPLES_DIR / filename).read_text().strip()


# ------------------------------------------------------------------
# TP_HIT
# ------------------------------------------------------------------

class TestTpHit:

    def test_tp_hit_01_sei_tp1(self):
        r = parse_tp_hit(_load("tp_hit_01.txt"))
        assert r.pair == "SEI/USDT"
        assert r.trade_id == 1256
        assert r.tp_number == 1
        assert r.profit_pct == 16.03
        assert r.period == "23 Minutes"

    def test_tp_hit_02_inj_tp2(self):
        r = parse_tp_hit(_load("tp_hit_02.txt"))
        assert r.pair == "INJ/USDT"
        assert r.trade_id == 1248
        assert r.tp_number == 2
        assert r.profit_pct == 96.03
        assert r.period == "1 Hours 32 Minutes"

    def test_tp_hit_03_inj_tp1_fast(self):
        r = parse_tp_hit(_load("tp_hit_03.txt"))
        assert r.pair == "INJ/USDT"
        assert r.trade_id == 1248
        assert r.tp_number == 1
        assert r.profit_pct == 41.81
        assert r.period == "1 Minutes"


# ------------------------------------------------------------------
# ALL_TP_HIT
# ------------------------------------------------------------------

class TestAllTpHit:

    def test_all_tp_hit_01_bch(self):
        r = parse_all_tp_hit(_load("all_tp_hit_01.txt"))
        assert r.pair == "BCH/USDT"
        assert r.trade_id == 1284
        assert r.profit_pct == 282.76
        assert r.period == "9 Hours 39 Minutes"

    def test_all_tp_hit_02_tia(self):
        r = parse_all_tp_hit(_load("all_tp_hit_02.txt"))
        assert r.pair == "TIA/USDT"
        assert r.trade_id == 1283
        assert r.profit_pct == 226.98

    def test_all_tp_hit_03_pol(self):
        r = parse_all_tp_hit(_load("all_tp_hit_03.txt"))
        assert r.pair == "POL/USDT"
        assert r.trade_id == 1281
        assert r.profit_pct == 395.97
        assert r.period == "13 Hours 34 Minutes"


# ------------------------------------------------------------------
# BREAKEVEN
# ------------------------------------------------------------------

class TestBreakeven:

    def test_breakeven_01_zk_tp1(self):
        r = parse_breakeven(_load("breakeven_01.txt"))
        assert r.pair == "ZK/USDT"
        assert r.trade_id == 1286
        assert r.tp_secured == 1

    def test_breakeven_02_atom_tp1(self):
        r = parse_breakeven(_load("breakeven_02.txt"))
        assert r.pair == "ATOM/USDT"
        assert r.trade_id == 1285
        assert r.tp_secured == 1

    def test_breakeven_03_sei_tp2(self):
        r = parse_breakeven(_load("breakeven_03.txt"))
        assert r.pair == "SEI/USDT"
        assert r.trade_id == 1256
        assert r.tp_secured == 2

    def test_breakeven_04_crv_tp2(self):
        r = parse_breakeven(_load("breakeven_04.txt"))
        assert r.pair == "CRV/USDT"
        assert r.trade_id == 1250
        assert r.tp_secured == 2


# ------------------------------------------------------------------
# STOP_HIT
# ------------------------------------------------------------------

class TestStopHit:

    def test_stop_hit_01_wif(self):
        r = parse_stop_hit(_load("stop_hit_01.txt"))
        assert r.pair == "WIF/USDT"
        assert r.trade_id == 1267
        assert r.loss_pct == -77.7


# ------------------------------------------------------------------
# CANCELED
# ------------------------------------------------------------------

class TestCanceled:

    def test_canceled_01_render_with_pair(self):
        r = parse_canceled(_load("canceled_01.txt"))
        assert r.trade_id == 1265
        assert r.pair == "RENDER/USDT"
        assert "delay" in r.reason.lower() or "fast moving" in r.reason.lower()

    def test_canceled_02_no_pair(self):
        r = parse_canceled(_load("canceled_02.txt"))
        assert r.trade_id == 1268
        assert "requirements" in r.reason.lower()

    def test_canceled_03_no_pair(self):
        r = parse_canceled(_load("canceled_03.txt"))
        assert r.trade_id == 1252

    def test_canceled_04_dot_inline(self):
        """CANCEL DOT/USDT #1249 (price moved too fast) — inline format."""
        r = parse_canceled(_load("canceled_04.txt"))
        assert r.trade_id == 1249
        assert r.pair == "DOT/USDT"
        assert "price moved" in r.reason.lower()

    def test_canceled_05(self):
        r = parse_canceled(_load("canceled_05.txt"))
        assert r.trade_id == 1250


# ------------------------------------------------------------------
# TRADE_CLOSED
# ------------------------------------------------------------------

class TestTradeClosed:

    def test_trade_closed_01_inj(self):
        r = parse_trade_closed(_load("trade_closed_01.txt"))
        assert r.pair == "INJ/USDT"
        assert r.trade_id == 1253
        assert "TAKE PROFIT 2" in r.detail.upper()

    def test_trade_closed_02_apt_with_emoji(self):
        r = parse_trade_closed(_load("trade_closed_02.txt"))
        assert r.pair == "APT/USDT"
        assert r.trade_id == 1234
        assert "TAKE PROFIT 2" in r.detail.upper()


# ------------------------------------------------------------------
# PREPARATION
# ------------------------------------------------------------------

class TestPreparation:

    def test_preparation_01_bch(self):
        r = parse_preparation(_load("preparation_01.txt"))
        assert r.trade_id == 1284
        assert r.pair == "BCH/USDT"
        assert r.side == "SHORT"
        assert r.entry == 515.0
        assert r.leverage == 27

    def test_preparation_02_zk(self):
        r = parse_preparation(_load("preparation_02.txt"))
        assert r.trade_id == 1286
        assert r.pair == "ZK/USDT"
        assert r.side == "SHORT"
        assert r.entry == 0.02153
        assert r.leverage == 14

    def test_preparation_03_doge_no_entry(self):
        """preparation_03 has no ENTRY field."""
        r = parse_preparation(_load("preparation_03.txt"))
        assert r.trade_id == 1260
        assert r.pair == "DOGE/USDT"
        assert r.side == "LONG"
        assert r.entry is None
        assert r.leverage == 27

    def test_preparation_04_eth_no_entry(self):
        r = parse_preparation(_load("preparation_04.txt"))
        assert r.trade_id == 1255
        assert r.pair == "ETH/USDT"
        assert r.side == "SHORT"
        assert r.entry is None
        assert r.leverage == 27


# ------------------------------------------------------------------
# MANUAL_UPDATE
# ------------------------------------------------------------------

class TestManualUpdate:

    def test_manual_update_01_ada(self):
        r = parse_manual_update(_load("manual_update_01.txt"))
        assert r.trade_id == 1259
        assert r.pair == "ADA/USDT"
        assert "LIMIT" in r.instruction.upper()


# ------------------------------------------------------------------
# Error handling
# ------------------------------------------------------------------

class TestUpdateParserErrors:

    def test_tp_hit_missing_pair_raises(self):
        with pytest.raises(UpdateParseError):
            parse_tp_hit("TP TARGET 1 HIT\nPROFIT: 10%")

    def test_stop_hit_missing_loss_raises(self):
        with pytest.raises(UpdateParseError):
            parse_stop_hit("STOP TARGET HIT\nPAIR: BTC/USDT #1234")

    def test_canceled_missing_trade_id_raises(self):
        with pytest.raises(UpdateParseError):
            parse_canceled("Some random canceled message")

    def test_preparation_missing_pair_raises(self):
        with pytest.raises(UpdateParseError):
            parse_preparation("Trade #1234 Incoming...\n(Prepare, dont place it yet)")


# ------------------------------------------------------------------
# SL_UPDATE (parse_sl_update)
# ------------------------------------------------------------------

class TestSlUpdate:

    def test_move_sl_to(self):
        r = parse_sl_update("Trade #1286 — Move SL to 1985")
        assert r is not None
        assert r.trade_id == 1286
        assert r.new_price == 1985.0

    def test_adjust_stop_loss_to(self):
        r = parse_sl_update("Trade #1250 Adjust stop loss to 0.025")
        assert r is not None
        assert r.trade_id == 1250
        assert r.new_price == 0.025

    def test_new_sl_colon(self):
        r = parse_sl_update("#1284 New SL: 510.5")
        assert r is not None
        assert r.trade_id == 1284
        assert r.new_price == 510.5

    def test_sl_arrow(self):
        r = parse_sl_update("Trade #1260 SL → 0.178")
        assert r is not None
        assert r.trade_id == 1260
        assert r.new_price == 0.178

    def test_sl_dash_arrow(self):
        r = parse_sl_update("#1260 SL -> 0.178")
        assert r is not None
        assert r.trade_id == 1260
        assert r.new_price == 0.178

    def test_change_sl(self):
        r = parse_sl_update("Trade #1234 change SL to 45000")
        assert r is not None
        assert r.trade_id == 1234
        assert r.new_price == 45000.0

    def test_set_stop_loss(self):
        r = parse_sl_update("#1234 set stop loss 2000.5")
        assert r is not None
        assert r.trade_id == 1234
        assert r.new_price == 2000.5

    def test_update_sl(self):
        r = parse_sl_update("Trade #1280 update SL to 3.45")
        assert r is not None
        assert r.trade_id == 1280
        assert r.new_price == 3.45

    def test_no_trade_id_returns_none(self):
        assert parse_sl_update("Move SL to 1985") is None

    def test_no_sl_instruction_returns_none(self):
        assert parse_sl_update("Trade #1286 — close half the position") is None

    def test_unrelated_message_returns_none(self):
        assert parse_sl_update("Great job everyone, TP1 hit!") is None
