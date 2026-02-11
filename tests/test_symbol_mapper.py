"""Tests for the symbol mapper — Potion Perps pair → Hyperliquid coin."""

import pytest

from src.utils.symbol_mapper import (
    HYPERLIQUID_COINS,
    potion_to_hyperliquid,
    get_all_mappings,
)


# ------------------------------------------------------------------
# Direct 1:1 mappings (strip /USDT, use base)
# ------------------------------------------------------------------

class TestDirectMappings:

    @pytest.mark.parametrize("pair,expected", [
        ("BTC/USDT", "BTC"),
        ("ETH/USDT", "ETH"),
        ("SOL/USDT", "SOL"),
        ("DOGE/USDT", "DOGE"),
        ("ADA/USDT", "ADA"),
        ("AVAX/USDT", "AVAX"),
        ("NEAR/USDT", "NEAR"),
        ("ATOM/USDT", "ATOM"),
        ("APT/USDT", "APT"),
        ("SUI/USDT", "SUI"),
        ("OP/USDT", "OP"),
        ("ARB/USDT", "ARB"),
        ("INJ/USDT", "INJ"),
        ("TIA/USDT", "TIA"),
        ("WIF/USDT", "WIF"),
        ("RENDER/USDT", "RENDER"),
        ("ZK/USDT", "ZK"),
        ("POL/USDT", "POL"),
        ("HYPE/USDT", "HYPE"),
        ("TON/USDT", "TON"),
    ])
    def test_direct(self, pair, expected):
        assert potion_to_hyperliquid(pair) == expected


# ------------------------------------------------------------------
# Kilo-prefix: 1000X → kX
# ------------------------------------------------------------------

class TestKiloPrefix:

    @pytest.mark.parametrize("pair,expected", [
        ("1000BONK/USDT", "kBONK"),
        ("1000PEPE/USDT", "kPEPE"),
        ("1000SHIB/USDT", "kSHIB"),
        ("1000LUNC/USDT", "kLUNC"),
        ("1000NEIRO/USDT", "kNEIRO"),
        ("1000DOGS/USDT", "kDOGS"),
    ])
    def test_kilo(self, pair, expected):
        assert potion_to_hyperliquid(pair) == expected


# ------------------------------------------------------------------
# Bare meme coins (without 1000 prefix) → kX via overrides
# ------------------------------------------------------------------

class TestBareMemeCoins:

    @pytest.mark.parametrize("pair,expected", [
        ("BONK/USDT", "kBONK"),
        ("PEPE/USDT", "kPEPE"),
        ("SHIB/USDT", "kSHIB"),
        ("LUNC/USDT", "kLUNC"),
        ("NEIRO/USDT", "kNEIRO"),
        ("DOGS/USDT", "kDOGS"),
    ])
    def test_bare_meme(self, pair, expected):
        assert potion_to_hyperliquid(pair) == expected


# ------------------------------------------------------------------
# Rebrands
# ------------------------------------------------------------------

class TestRebrands:

    def test_matic_to_pol(self):
        assert potion_to_hyperliquid("MATIC/USDT") == "POL"

    def test_ftm_to_sonic(self):
        assert potion_to_hyperliquid("FTM/USDT") == "S"

    def test_rndr_to_render(self):
        assert potion_to_hyperliquid("RNDR/USDT") == "RENDER"

    def test_jellyjelly_to_jelly(self):
        assert potion_to_hyperliquid("JELLYJELLY/USDT") == "JELLY"


# ------------------------------------------------------------------
# All signal sample pairs
# ------------------------------------------------------------------

class TestSamplePairs:

    @pytest.mark.parametrize("pair,expected", [
        ("1000BONK/USDT", "kBONK"),
        ("ADA/USDT", "ADA"),
        ("APT/USDT", "APT"),
        ("ATOM/USDT", "ATOM"),
        ("BCH/USDT", "BCH"),
        ("CRV/USDT", "CRV"),
        ("DOGE/USDT", "DOGE"),
        ("DOT/USDT", "DOT"),
        ("ETH/USDT", "ETH"),
        ("INJ/USDT", "INJ"),
        ("POL/USDT", "POL"),
        ("RENDER/USDT", "RENDER"),
        ("SEI/USDT", "SEI"),
        ("TIA/USDT", "TIA"),
        ("WIF/USDT", "WIF"),
        ("XRP/USDT", "XRP"),
        ("ZK/USDT", "ZK"),
    ])
    def test_sample_pair(self, pair, expected):
        assert potion_to_hyperliquid(pair) == expected


# ------------------------------------------------------------------
# Validation against available coins
# ------------------------------------------------------------------

class TestValidation:

    def test_valid_coin_passes(self):
        available = {"ETH": {}, "BTC": {}}
        assert potion_to_hyperliquid("ETH/USDT", available) == "ETH"

    def test_invalid_coin_raises(self):
        available = {"ETH": {}, "BTC": {}}
        with pytest.raises(ValueError, match="not available on Hyperliquid"):
            potion_to_hyperliquid("FAKECOIN/USDT", available)

    def test_rebrand_validates_new_name(self):
        available = {"POL": {}}
        assert potion_to_hyperliquid("MATIC/USDT", available) == "POL"

    def test_rebrand_fails_if_new_name_missing(self):
        available = {"MATIC": {}}  # old name, not new
        with pytest.raises(ValueError):
            potion_to_hyperliquid("FTM/USDT", available)  # S not in available


# ------------------------------------------------------------------
# Edge cases
# ------------------------------------------------------------------

class TestEdgeCases:

    def test_lowercase_pair(self):
        assert potion_to_hyperliquid("eth/usdt") == "ETH"

    def test_extra_whitespace(self):
        assert potion_to_hyperliquid("  ETH / USDT  ") == "ETH"

    def test_unknown_coin_passthrough(self):
        assert potion_to_hyperliquid("NEWCOIN/USDT") == "NEWCOIN"

    def test_unknown_kilo_coin(self):
        assert potion_to_hyperliquid("1000UNKNOWN/USDT") == "kUNKNOWN"

    def test_get_all_mappings_returns_dict(self):
        mappings = get_all_mappings()
        assert isinstance(mappings, dict)
        assert len(mappings) > 100
        assert mappings["MATIC"] == "POL"
        assert mappings["BONK"] == "kBONK"
