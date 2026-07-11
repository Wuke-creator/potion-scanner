"""Tests for the cabal-chat entry parser.

Every positive case is a real d3lta_0 message shape observed June-July 2026
(lightly trimmed). Negatives are the management chatter that MUST NOT parse:
a false positive here proposes a real trade.
"""

from __future__ import annotations

from src.parser.cabal_parser import parse_cabal_entry

YGG = (
    "Taking a risky short on Ygg/Usdt at cmp ( 0.02240$)  \n"
    "Dca : 0.02310$  \n"
    "Sl : 1h candle close above 0.02360$  \n"
    "Final tp : 0.019$  \n"
    "Tp 1 : 0.02160$  \n\n"
    "Note : It's a risky scalp. So I would recommend y'all to use low "
    "margin with low lev.   As it's risky."
)

RUNE = (
    "Longing rune /usdt at cmp ( 0.3895$ )  \n"
    "Dca : 0.3730$  \n"
    "Sl : 1h candle close below 0.3650$  \n"
    "Final tp : 0.4650$+  \n"
    "Risk : 0.25RR  \n"
    "Tp 1 : 0.4030$  \n"
    " Use low lev and low margins"
)

BTC = (
    "Scalp long BTC at CMP ( 62693$)  \n"
    "Dca: 61730$  \n"
    "Sl : 15min close below  60970$  \n"
    "Final tp : 67400$  \n"
    "Risk: 0.5RR"
)

PYTH = (
    "Token : Pyth / Usdt  \n"
    "Entry : 0.04370$  \n"
    "Dca : 0.04240$  \n"
    "Sl : 15min candle close below 0.04140$  \n"
    "Final tp : 0.050$  \n"
    "Risk : 0.5 RR  \n"
    "Total set-up : 1:3RR"
)

LUNC = (
    "Longing for a risky trade on LUNC at CMP (  0.06470$)  \n"
    "Dca: 0.06160$  \n"
    "Sl: 0.05930$  \n"
    "Final tp : 0.093$  \n"
    "Tp 1: 0.06780$  \n"
    "Risk: 0.25RR"
)


class TestRealEntries:
    def test_ygg_short_conditional_sl(self):
        s = parse_cabal_entry(YGG)
        assert s is not None
        assert s.pair == "YGG/USDT" and s.side == "SHORT"
        assert s.entry == 0.02240
        assert s.stop_loss == 0.02360 and s.stop_is_conditional
        # nearest first: tp1 then final
        assert s.take_profits == [0.02160, 0.019]
        assert s.leverage is None  # "low lev" has no number

    def test_rune_long(self):
        s = parse_cabal_entry(RUNE)
        assert s is not None
        assert s.pair == "RUNE/USDT" and s.side == "LONG"
        assert s.entry == 0.3895
        assert s.stop_loss == 0.3650 and s.stop_is_conditional
        assert s.take_profits == [0.4030, 0.4650]

    def test_btc_scalp_long_no_tp1(self):
        s = parse_cabal_entry(BTC)
        assert s is not None
        assert s.pair == "BTC/USDT" and s.side == "LONG"
        assert s.entry == 62693
        assert s.stop_loss == 60970 and s.stop_is_conditional
        assert s.take_profits == [67400]

    def test_pyth_token_block_infers_long(self):
        s = parse_cabal_entry(PYTH)
        assert s is not None
        assert s.pair == "PYTH/USDT"
        assert s.side == "LONG" and s.side_inferred  # SL below entry
        assert s.entry == 0.04370
        assert s.stop_loss == 0.04140
        assert s.take_profits == [0.050]

    def test_lunc_hard_sl_not_conditional(self):
        s = parse_cabal_entry(LUNC)
        assert s is not None
        assert s.pair == "LUNC/USDT" and s.side == "LONG"
        assert s.stop_loss == 0.05930 and not s.stop_is_conditional
        assert s.take_profits == [0.06780, 0.093]

    def test_explicit_leverage_copied(self):
        s = parse_cabal_entry(LUNC + "\nUsing 20x here")
        assert s is not None and s.leverage == 20


class TestManagementChatterDoesNotParse:
    def test_taking_tp(self):
        assert parse_cabal_entry("Taking tp 1 on PYTH 4% up from entry and almost hit 1RR") is None

    def test_taking_tp2_rr(self):
        assert parse_cabal_entry("9% up so taking tp 2. Almost 2RR gains.") is None

    def test_moving_sl_be(self):
        assert parse_cabal_entry("Taking tp 1 and moving sl on be") is None

    def test_trimming(self):
        assert parse_cabal_entry("Trimming 15% and moving sl on be for safety") is None

    def test_closing_dca(self):
        assert parse_cabal_entry("Closing the dca amount and moving sl on be.") is None

    def test_tp_hit(self):
        assert parse_cabal_entry("Rune hits tp 1 perfectly") is None

    def test_streams_and_chat(self):
        assert parse_cabal_entry(
            "Good evening ladies & gents, I'll be streaming live on twitch at 0:00 CEST"
        ) is None
        assert parse_cabal_entry("btc hits 2 RR \U0001f525") is None

    def test_thesis_without_sl(self):
        # pidgeon-style commentary: direction words but no SL price
        assert parse_cabal_entry(
            "if we get to $192 that'll be another nice level to add to shorts"
        ) is None
        assert parse_cabal_entry(
            "these are long term swing shorts for me so super low leverage, "
            "the goal is to get a short average as high as possible"
        ) is None

    def test_sl_side_sanity_rejects_garbage(self):
        # long with SL above entry is not a valid parse
        assert parse_cabal_entry(
            "Longing FOO/USDT at cmp ( 1.00$ ) Sl : 1.10$ Final tp : 1.5$"
        ) is None
