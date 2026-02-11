"""Discord adapter — listens to a specific channel for signals from a source bot.

Uses discord.py which handles reconnection, heartbeats, and rate limits
automatically. Requires the message_content privileged intent to be enabled
in the Discord developer portal.
"""

import asyncio
import logging

import discord

from src.input.base_adapter import BaseAdapter

logger = logging.getLogger(__name__)


class DiscordAdapter(BaseAdapter):
    """Reads trading signals from a Discord channel.

    Filters messages by channel ID and source bot name, then pushes
    the message content onto the queue for pipeline processing.

    Args:
        bot_token: Discord bot authentication token.
        channel_id: ID of the channel to listen on.
        source_bot_name: Display name of the bot whose messages to capture.
        queue: Optional asyncio.Queue (created automatically if not provided).
    """

    def __init__(
        self,
        bot_token: str,
        channel_id: str,
        source_bot_name: str = "Potion Perps",
        queue: asyncio.Queue | None = None,
    ):
        super().__init__(queue)
        self._bot_token = bot_token
        self._channel_id = int(channel_id) if channel_id else 0
        self._source_bot_name = source_bot_name

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)

        self._setup_handlers()

    def _setup_handlers(self) -> None:
        @self._client.event
        async def on_ready():
            logger.info(
                "Discord adapter connected as %s (watching channel %s for '%s')",
                self._client.user, self._channel_id, self._source_bot_name,
            )

        @self._client.event
        async def on_message(message: discord.Message):
            # Ignore messages from ourselves
            if message.author == self._client.user:
                return

            # Filter by channel
            if message.channel.id != self._channel_id:
                return

            # Filter by source bot name
            if message.author.display_name != self._source_bot_name:
                return

            text = message.content.strip()
            if not text:
                return

            logger.debug(
                "Discord message from %s in #%s (%d chars)",
                message.author.display_name, message.channel.name, len(text),
            )
            await self._queue.put(text)

    async def start(self) -> None:
        """Connect to Discord and start listening. Blocks until disconnected."""
        logger.info("Starting Discord adapter (channel=%s, source='%s')",
                     self._channel_id, self._source_bot_name)
        await self._client.start(self._bot_token)

    async def stop(self) -> None:
        """Disconnect from Discord."""
        logger.info("Stopping Discord adapter")
        await self._client.close()
