"""Tests for Discord-specific text cleanup before Telegram forwarding.

Covers:
  - Role mentions (<@&id>) get stripped, no raw blob in output
  - User mentions (<@id> and <@!id>) get stripped
  - Channel mentions (<#id>) get stripped
  - Custom emoji (<:name:id> / <a:name:id>) become :name:
  - Bare Discord message URLs become 'View on Discord' hyperlinks
  - Multiple consecutive blank lines collapse after strips
  - The empty-pointer guard correctly identifies pointer posts as empty
"""
from __future__ import annotations

from src.formatter import discord_to_telegram_html
from src.router import Router


# ---------------------------------------------------------------------------
# Mention + emoji cleanup
# ---------------------------------------------------------------------------

def test_role_mention_stripped():
    out = discord_to_telegram_html("Trading Signal Alert\n<@&1316518702790742059>\nhello")
    assert "<@&" not in out
    assert "1316518702790742059" not in out
    assert "Trading Signal Alert" in out
    assert "hello" in out


def test_user_mention_stripped():
    out = discord_to_telegram_html("ping <@123456789012345678> please")
    assert "<@" not in out
    assert "ping" in out
    assert "please" in out


def test_user_nick_mention_stripped():
    # <@!id> is the legacy nickname-mention form
    out = discord_to_telegram_html("hey <@!987654321> are you there")
    assert "<@!" not in out
    assert "987654321" not in out


def test_channel_mention_stripped():
    out = discord_to_telegram_html("see <#1316518499283370064> for more")
    assert "<#" not in out
    assert "1316518499283370064" not in out


def test_custom_emoji_normalised():
    # <:rocket_purple:123> should become :rocket_purple:
    out = discord_to_telegram_html("Big launch <:rocket_purple:1234567890>!")
    assert "<:" not in out
    assert "1234567890" not in out
    assert ":rocket_purple:" in out


def test_animated_emoji_normalised():
    out = discord_to_telegram_html("Vibing <a:partyparrot:9999>!")
    assert "<a:" not in out
    assert ":partyparrot:" in out


def test_multiple_strips_collapse_blank_lines():
    raw = (
        "Trading Signal Alert\n"
        "<@&1316518702790742059>\n"
        "<@&9999999999999999>\n"
        "<#111>\n"
        "https://discord.com/channels/1/2/3"
    )
    out = discord_to_telegram_html(raw)
    # No raw mention syntax should leak
    assert "<@&" not in out
    assert "<#" not in out
    # No 3+ newlines in a row (cleanup collapses them to max 2)
    assert "\n\n\n" not in out


# ---------------------------------------------------------------------------
# Discord URL → "View on Discord" hyperlink
# ---------------------------------------------------------------------------

def test_discord_message_url_becomes_clean_link():
    raw = "https://discord.com/channels/1260259552763580537/1340469776815886379"
    out = discord_to_telegram_html(raw)
    assert "View on Discord" in out
    assert "<a href=" in out
    # Original URL should be in the href, not as bare text
    assert raw in out  # in the href attr


def test_discord_message_url_with_message_id_works():
    raw = "https://discord.com/channels/1/2/3"
    out = discord_to_telegram_html(raw)
    assert "View on Discord" in out


def test_non_discord_urls_unaffected():
    raw = "Visit https://google.com please"
    out = discord_to_telegram_html(raw)
    # Non-Discord URLs do not get the View-on-Discord treatment
    assert "View on Discord" not in out
    assert "https://google.com" in out


# ---------------------------------------------------------------------------
# Empty-pointer guard (router-level decision to drop or forward)
# ---------------------------------------------------------------------------

def test_pointer_message_detected_as_empty():
    raw = (
        "Trading Signal Alert\n"
        "<@&1316518702790742059>\n"
        "https://discord.com/channels/1260259552763580537/1340469776815886379"
    )
    assert Router._is_empty_signal_pointer(raw) is True


def test_real_signal_not_detected_as_empty():
    raw = (
        "TRADING SIGNAL ALERT\n"
        "PAIR: WET/USDT #1234 (HIGH RISK)\n"
        "TYPE: SCALP\n"
        "SIZE: 1-4%\n"
        "SIDE: SHORT\n"
        "ENTRY: 0.099\n"
        "SL: 0.105\n"
        "TP1: 0.094 (5%)\n"
        "TP2: 0.090 (9%)\n"
        "TP3: 0.085 (14%)\n"
        "LEVERAGE: 50x"
    )
    assert Router._is_empty_signal_pointer(raw) is False


def test_completely_empty_treated_as_empty():
    assert Router._is_empty_signal_pointer("") is True
    assert Router._is_empty_signal_pointer("   \n  ") is True


def test_header_only_treated_as_empty():
    # Just the header phrase, no fields, no numbers
    assert Router._is_empty_signal_pointer("Trading Signal Alert") is True


def test_pointer_with_only_role_mention():
    raw = "<@&12345>\nTrading Signal Alert"
    assert Router._is_empty_signal_pointer(raw) is True
