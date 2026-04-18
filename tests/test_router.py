"""Tests for src/router.py — channel ID → ref link routing + classification."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.config import (
    SOURCE_MEMECOIN,
    SOURCE_PERPS,
    ChannelRoute,
    DiscordConfig,
)
from src.discord_listener import IncomingMessage
from src.router import Router

SAMPLES_DIR = Path("signals/samples")


def _load(filename: str) -> str:
    return (SAMPLES_DIR / filename).read_text(encoding="utf-8").strip()


PERPS_LINK = "https://partner.blofin.com/d/potion"
MEMECOIN_LINK = "https://trade.padre.gg/rk/orangie"

PERP_BOT_ID = 1445440392509132850
MANUAL_PERP_ID = 1316518499283370064
PREDICTION_ID = 1420272690459181118


def _make_discord_config() -> DiscordConfig:
    return DiscordConfig(
        bot_token="fake",
        guild_id=1260259552763580537,
        channels=[
            ChannelRoute(
                channel_id=PERP_BOT_ID,
                key="perp_bot",
                name="Perp Bot Calls",
                source_type=SOURCE_PERPS,
                ref_link=PERPS_LINK,
            ),
            ChannelRoute(
                channel_id=MANUAL_PERP_ID,
                key="manual_perp",
                name="Manual Perp Calls",
                source_type=SOURCE_PERPS,
                ref_link=PERPS_LINK,
            ),
            ChannelRoute(
                channel_id=PREDICTION_ID,
                key="prediction",
                name="Prediction Calls",
                source_type=SOURCE_MEMECOIN,
                ref_link=MEMECOIN_LINK,
            ),
        ],
    )


def _make_dispatcher_mock() -> AsyncMock:
    dispatcher = AsyncMock()
    dispatcher.dispatch = AsyncMock(return_value=None)
    return dispatcher


def _msg(channel_id: int, content: str, channel_name: str = "channel") -> IncomingMessage:
    return IncomingMessage(
        channel_id=channel_id,
        channel_name=channel_name,
        author_name="bot",
        author_is_bot=True,
        content=content,
    )


def _dispatched_text(dispatcher: AsyncMock) -> str:
    """Extract the text passed to dispatcher.dispatch (called with kwargs)."""
    return dispatcher.dispatch.await_args.kwargs["text"]


def _dispatched_source_key(dispatcher: AsyncMock) -> str:
    return dispatcher.dispatch.await_args.kwargs["source_key"]


@pytest.mark.asyncio
class TestRouting:
    async def test_perp_bot_signal_routes_correctly(self):
        dispatcher = _make_dispatcher_mock()
        router = Router(_make_discord_config(), dispatcher)

        await router.handle(_msg(PERP_BOT_ID, _load("signal_alert_01.txt")))

        dispatcher.dispatch.assert_awaited_once()
        kwargs = dispatcher.dispatch.await_args.kwargs
        assert "Perp Bot Calls" in kwargs["text"]
        assert kwargs["source_key"] == "perp_bot"
        assert kwargs["pair"] == "ZK/USDT"
        assert kwargs["keyboard"] is not None

    async def test_manual_perp_channel_routes_correctly(self):
        dispatcher = _make_dispatcher_mock()
        router = Router(_make_discord_config(), dispatcher)

        await router.handle(_msg(MANUAL_PERP_ID, _load("signal_alert_01.txt")))

        dispatcher.dispatch.assert_awaited_once()
        kwargs = dispatcher.dispatch.await_args.kwargs
        assert "Perp Bot Calls" not in kwargs["text"]  # different channel name
        assert kwargs["source_key"] == "manual_perp"

    async def test_prediction_channel_routes_correctly(self):
        dispatcher = _make_dispatcher_mock()
        router = Router(_make_discord_config(), dispatcher)

        await router.handle(_msg(PREDICTION_ID, _load("signal_alert_01.txt")))

        dispatcher.dispatch.assert_awaited_once()
        kwargs = dispatcher.dispatch.await_args.kwargs
        assert "Prediction Calls" in kwargs["text"]
        assert kwargs["source_key"] == "prediction"

    async def test_unknown_channel_id_is_dropped(self):
        dispatcher = _make_dispatcher_mock()
        router = Router(_make_discord_config(), dispatcher)

        await router.handle(_msg(99999999, "TRADING SIGNAL ALERT"))

        dispatcher.dispatch.assert_not_called()


@pytest.mark.asyncio
class TestClassification:
    async def test_noise_message_dropped(self):
        dispatcher = _make_dispatcher_mock()
        router = Router(_make_discord_config(), dispatcher)

        await router.handle(_msg(PERP_BOT_ID, _load("noise_01.txt")))

        dispatcher.dispatch.assert_not_called()

    async def test_preparation_message_dropped(self):
        dispatcher = _make_dispatcher_mock()
        router = Router(_make_discord_config(), dispatcher)

        await router.handle(_msg(PERP_BOT_ID, _load("preparation_01.txt")))

        dispatcher.dispatch.assert_not_called()

    async def test_tp_hit_forwarded_with_label(self):
        dispatcher = _make_dispatcher_mock()
        router = Router(_make_discord_config(), dispatcher)

        await router.handle(_msg(PERP_BOT_ID, _load("tp_hit_01.txt")))

        dispatcher.dispatch.assert_awaited_once()
        assert "Take Profit" in _dispatched_text(dispatcher)

    async def test_unknown_text_dropped_in_perps_channel(self):
        dispatcher = _make_dispatcher_mock()
        router = Router(_make_discord_config(), dispatcher)

        await router.handle(_msg(PERP_BOT_ID, "wagmi"))

        dispatcher.dispatch.assert_not_called()

    async def test_unknown_text_forwarded_in_memecoin_channel(self):
        dispatcher = _make_dispatcher_mock()
        router = Router(_make_discord_config(), dispatcher)

        await router.handle(_msg(PREDICTION_ID, "long $PEPE here, breaking out"))

        dispatcher.dispatch.assert_awaited_once()
        sent = _dispatched_text(dispatcher)
        assert "long $PEPE here" in sent
        assert MEMECOIN_LINK in sent


@pytest.mark.asyncio
class TestErrorHandling:
    async def test_router_never_raises_when_dispatcher_throws(self):
        dispatcher = AsyncMock()
        dispatcher.dispatch = AsyncMock(side_effect=RuntimeError("kaboom"))
        router = Router(_make_discord_config(), dispatcher)

        # Must not raise — error is caught and logged
        await router.handle(_msg(PERP_BOT_ID, _load("signal_alert_01.txt")))
