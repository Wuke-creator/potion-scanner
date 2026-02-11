"""Tests for the Discord adapter."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.input.discord_adapter import DiscordAdapter


def _make_message(
    author_name: str = "Potion Perps",
    channel_id: int = 12345,
    content: str = "TRADING SIGNAL ALERT",
    is_self: bool = False,
):
    """Create a mock Discord message."""
    msg = MagicMock()
    msg.author.display_name = author_name
    msg.channel.id = channel_id
    msg.channel.name = "signals"
    msg.content = content
    return msg, is_self


class TestDiscordAdapter:
    def test_init(self):
        with patch("src.input.discord_adapter.discord.Client"):
            adapter = DiscordAdapter(
                bot_token="token",
                channel_id="12345",
                source_bot_name="Potion Perps",
            )
            assert adapter._channel_id == 12345
            assert adapter._source_bot_name == "Potion Perps"

    def test_init_empty_channel_id(self):
        with patch("src.input.discord_adapter.discord.Client"):
            adapter = DiscordAdapter(
                bot_token="token",
                channel_id="",
                source_bot_name="Test",
            )
            assert adapter._channel_id == 0


class TestMessageFiltering:
    """Test the on_message filtering logic directly."""

    @pytest.mark.asyncio
    async def test_correct_channel_and_author_queued(self):
        queue = asyncio.Queue()

        with patch("src.input.discord_adapter.discord.Client") as MockClient:
            mock_client = MockClient.return_value
            mock_client.user = MagicMock()
            mock_client.user.__eq__ = lambda self, other: False

            adapter = DiscordAdapter(
                bot_token="token",
                channel_id="12345",
                source_bot_name="Potion Perps",
                queue=queue,
            )

            # Get the on_message handler that was registered
            on_message = None
            for call in mock_client.event.call_args_list:
                func = call[0][0]
                if func.__name__ == "on_message":
                    on_message = func
                    break

            assert on_message is not None

            # Create matching message
            msg = MagicMock()
            msg.author = MagicMock()
            msg.author.display_name = "Potion Perps"
            msg.author.__eq__ = lambda self, other: False
            msg.channel.id = 12345
            msg.channel.name = "signals"
            msg.content = "TRADING SIGNAL ALERT\nBuy BTC"

            await on_message(msg)
            assert not queue.empty()
            assert await queue.get() == "TRADING SIGNAL ALERT\nBuy BTC"

    @pytest.mark.asyncio
    async def test_wrong_channel_filtered(self):
        queue = asyncio.Queue()

        with patch("src.input.discord_adapter.discord.Client") as MockClient:
            mock_client = MockClient.return_value
            mock_client.user = MagicMock()
            mock_client.user.__eq__ = lambda self, other: False

            adapter = DiscordAdapter(
                bot_token="token",
                channel_id="12345",
                source_bot_name="Potion Perps",
                queue=queue,
            )

            on_message = None
            for call in mock_client.event.call_args_list:
                func = call[0][0]
                if func.__name__ == "on_message":
                    on_message = func
                    break

            msg = MagicMock()
            msg.author = MagicMock()
            msg.author.display_name = "Potion Perps"
            msg.author.__eq__ = lambda self, other: False
            msg.channel.id = 99999  # Wrong channel
            msg.content = "signal"

            await on_message(msg)
            assert queue.empty()

    @pytest.mark.asyncio
    async def test_wrong_author_filtered(self):
        queue = asyncio.Queue()

        with patch("src.input.discord_adapter.discord.Client") as MockClient:
            mock_client = MockClient.return_value
            mock_client.user = MagicMock()
            mock_client.user.__eq__ = lambda self, other: False

            adapter = DiscordAdapter(
                bot_token="token",
                channel_id="12345",
                source_bot_name="Potion Perps",
                queue=queue,
            )

            on_message = None
            for call in mock_client.event.call_args_list:
                func = call[0][0]
                if func.__name__ == "on_message":
                    on_message = func
                    break

            msg = MagicMock()
            msg.author = MagicMock()
            msg.author.display_name = "Random User"  # Wrong author
            msg.author.__eq__ = lambda self, other: False
            msg.channel.id = 12345
            msg.content = "signal"

            await on_message(msg)
            assert queue.empty()

    @pytest.mark.asyncio
    async def test_empty_content_filtered(self):
        queue = asyncio.Queue()

        with patch("src.input.discord_adapter.discord.Client") as MockClient:
            mock_client = MockClient.return_value
            mock_client.user = MagicMock()
            mock_client.user.__eq__ = lambda self, other: False

            adapter = DiscordAdapter(
                bot_token="token",
                channel_id="12345",
                source_bot_name="Potion Perps",
                queue=queue,
            )

            on_message = None
            for call in mock_client.event.call_args_list:
                func = call[0][0]
                if func.__name__ == "on_message":
                    on_message = func
                    break

            msg = MagicMock()
            msg.author = MagicMock()
            msg.author.display_name = "Potion Perps"
            msg.author.__eq__ = lambda self, other: False
            msg.channel.id = 12345
            msg.content = "   "  # Whitespace only

            await on_message(msg)
            assert queue.empty()

    @pytest.mark.asyncio
    async def test_self_message_filtered(self):
        queue = asyncio.Queue()

        with patch("src.input.discord_adapter.discord.Client") as MockClient:
            mock_client = MockClient.return_value
            bot_user = MagicMock()
            mock_client.user = bot_user

            adapter = DiscordAdapter(
                bot_token="token",
                channel_id="12345",
                source_bot_name="Potion Perps",
                queue=queue,
            )

            on_message = None
            for call in mock_client.event.call_args_list:
                func = call[0][0]
                if func.__name__ == "on_message":
                    on_message = func
                    break

            msg = MagicMock()
            msg.author = bot_user  # Same as client.user
            msg.channel.id = 12345
            msg.content = "signal"

            await on_message(msg)
            assert queue.empty()
