"""Tests for Perp Pinger forwarding (the bot in #perp-calls-ostium).

The bot uses a non-Potion-template format ('SHORT WET @ 0.099',
'TP1 HIT AAVE', 'UPDATE WET', etc.) that the existing classifier flags
as NOISE. This test suite covers the recovery path:

  - perps source_type + bot author + classify == NOISE
    -> forward verbatim via format_unknown_message
    -> attach Trade-now keyboard with per-pair Ostium / Blofin deeplink
       extracted from the caption header
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.config import (
    SOURCE_PERPS,
    ChannelRoute,
    DiscordConfig,
)
from src.discord_listener import IncomingMessage
from src.formatter import _extract_pair_from_caption
from src.router import Router


# ---------------------------------------------------------------------------
# Pair extraction for Perp Pinger format
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("caption,expected", [
    ("SHORT WET @ 0.099 @Perp", "WET"),
    ("LONG MOODENG @ 0.5630-0.5650 @Perp", "MOODENG"),
    ("SHORT SATS @ 1400 @Perp\nRisky Short SATS", "SATS"),
    ("UPDATE WET @Perp WET Update: TP1 here and move SL to BE", "WET"),
    ("UPDATE TURBO @Perp TURBO Update: Adding a DCA", "TURBO"),
    ("UPDATE MOODENG @Perp MOODENG Update: Quick TP1 here", "MOODENG"),
    ("TP1 HIT AAVE @Perp Taking tp 1 on AAVE.", "AAVE"),
    ("SL HIT BTC @Perp", "BTC"),
    ("STOP HIT ETH @Perp", "ETH"),
])
def test_perp_pinger_ticker_extraction(caption, expected):
    assert _extract_pair_from_caption(caption) == expected


def test_extract_skips_blocklist_tokens():
    """Action words should NEVER be returned as a ticker."""
    # An empty header that resolves to nothing.
    assert _extract_pair_from_caption("UPDATE update text") != "UPDATE"
    assert _extract_pair_from_caption("SHORT SHORT") != "SHORT"


def test_extract_handles_potion_perps_bot_pair_format_too():
    """Original Potion Perps Bot uses PAIR: X/Y. Must still work."""
    assert _extract_pair_from_caption("PAIR: ETH/USDT #4242") == "ETH/USDT"


# ---------------------------------------------------------------------------
# Router: NOISE-from-bot on perps source forwards instead of dropping
# ---------------------------------------------------------------------------

def _route() -> ChannelRoute:
    return ChannelRoute(
        channel_id=1316518499283370064,
        key="manual_perp",
        name="Perp Calls",
        source_type=SOURCE_PERPS,
        ref_link="https://app.ostium.com/?ref=PTION",
    )


def _cfg() -> DiscordConfig:
    return DiscordConfig(
        bot_token="fake", guild_id=1260259552763580537,
        channels=[_route()],
    )


def _msg(content: str, *, is_bot: bool) -> IncomingMessage:
    return IncomingMessage(
        channel_id=1316518499283370064,
        channel_name="Perp Calls",
        author_name="Perp Pinger" if is_bot else "Some User",
        author_is_bot=is_bot,
        content=content,
    )


@pytest.mark.asyncio
async def test_perp_pinger_short_signal_forwards():
    """The exact SHORT SATS signal Luke flagged must reach the dispatcher."""
    raw = (
        "SHORT SATS @ 1400 @Perp\n"
        "Risky Short SATS\n"
        "EP: CMP(1400)[Adjust the zeros]\n"
        "SL: Hourly close above 1481\n"
        "DCA and TPs will be updated"
    )
    mock_dispatcher = AsyncMock()
    router = Router(
        discord_cfg=_cfg(), dispatcher=mock_dispatcher,
        analytics=None, open_signals=None,
    )
    await router._handle(_msg(raw, is_bot=True))
    assert mock_dispatcher.dispatch.call_count == 1
    call = mock_dispatcher.dispatch.call_args
    assert call.kwargs["pair"] == "SATS"
    # Trade-now keyboard should exist with per-pair Ostium deeplink
    keyboard = call.kwargs["keyboard"]
    assert keyboard is not None
    trade_btn = keyboard.inline_keyboard[0][0]
    assert "from=SATS" in trade_btn.url


@pytest.mark.asyncio
async def test_perp_pinger_tp1_hit_forwards_with_correct_ticker():
    """TP1 HIT AAVE must extract ticker AAVE (not TP1)."""
    mock_dispatcher = AsyncMock()
    router = Router(
        discord_cfg=_cfg(), dispatcher=mock_dispatcher,
        analytics=None, open_signals=None,
    )
    await router._handle(_msg("TP1 HIT AAVE @Perp Taking tp 1 on AAVE.", is_bot=True))
    assert mock_dispatcher.dispatch.call_count == 1
    assert mock_dispatcher.dispatch.call_args.kwargs["pair"] == "AAVE"


@pytest.mark.asyncio
async def test_perp_pinger_update_forwards():
    raw = "UPDATE TURBO @Perp TURBO Update: Adding a DCA here at 1.129. New avg entry is 1.141"
    mock_dispatcher = AsyncMock()
    router = Router(
        discord_cfg=_cfg(), dispatcher=mock_dispatcher,
        analytics=None, open_signals=None,
    )
    await router._handle(_msg(raw, is_bot=True))
    assert mock_dispatcher.dispatch.call_count == 1
    assert mock_dispatcher.dispatch.call_args.kwargs["pair"] == "TURBO"


@pytest.mark.asyncio
async def test_perp_human_chatter_still_drops():
    """A human posting random chatter in the perps channel must NOT
    forward — the bot-only filter prevents the channel from becoming
    a chat firehose."""
    mock_dispatcher = AsyncMock()
    router = Router(
        discord_cfg=_cfg(), dispatcher=mock_dispatcher,
        analytics=None, open_signals=None,
    )
    await router._handle(_msg(
        "lol that aave call was crazy", is_bot=False,
    ))
    assert mock_dispatcher.dispatch.call_count == 0


@pytest.mark.asyncio
async def test_perp_pinger_short_with_blofin_ref_uses_blofin_deeplink():
    """When the channel uses Blofin (Mac's Calls / Pingu Charts), the
    Trade-now URL should be the per-pair Blofin futures deeplink."""
    blofin_route = ChannelRoute(
        channel_id=1495099327004016791, key="macs_calls",
        name="Mac's Calls", source_type=SOURCE_PERPS,
        ref_link="https://partner.blofin.com/d/potion",
    )
    cfg = DiscordConfig(
        bot_token="fake", guild_id=1260259552763580537,
        channels=[blofin_route],
    )
    mock_dispatcher = AsyncMock()
    router = Router(
        discord_cfg=cfg, dispatcher=mock_dispatcher,
        analytics=None, open_signals=None,
    )
    msg = IncomingMessage(
        channel_id=1495099327004016791, channel_name="Mac's Calls",
        author_name="Mac's Bot", author_is_bot=True,
        content="SHORT WET @ 0.099 @Perp",
    )
    await router._handle(msg)
    assert mock_dispatcher.dispatch.call_count == 1
    keyboard = mock_dispatcher.dispatch.call_args.kwargs["keyboard"]
    assert keyboard is not None
    trade_btn = keyboard.inline_keyboard[0][0]
    assert "blofin.com/futures/WET-USDT" in trade_btn.url
