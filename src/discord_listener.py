"""Multi-channel Discord listener.

Connects to Discord with discord.py, subscribes to a configured set of
channel IDs, and pushes ``(channel_id, raw_message)`` tuples onto an
asyncio queue for downstream processing by the router.

discord.py handles reconnection, heartbeats, and rate limiting automatically.
The bot needs the Message Content privileged intent enabled in the Discord
developer portal.
"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass

import discord

logger = logging.getLogger(__name__)


@dataclass
class IncomingMessage:
    """A message captured from one of the monitored channels."""

    channel_id: int
    channel_name: str
    author_name: str
    author_is_bot: bool
    content: str


def _serialize_embed(embed: discord.Embed) -> str:
    """Flatten a Discord embed into a plain-text block usable by downstream
    Telegram senders. Uses minimal Telegram-HTML for structure (bold titles,
    italic footer) so the important fields are still visually distinct.

    Captures: author, title, description, fields (name/value pairs), footer,
    url. Skips thumbnails and images (Telegram DM can't easily inline them).
    """
    parts: list[str] = []
    if embed.author and embed.author.name:
        parts.append(html.escape(str(embed.author.name)))
    if embed.title:
        parts.append(f"<b>{html.escape(str(embed.title))}</b>")
    if embed.description:
        parts.append(html.escape(str(embed.description)))
    for field in embed.fields or []:
        fname = html.escape(str(field.name)) if field.name else ""
        fval = html.escape(str(field.value)) if field.value else ""
        if fname and fval:
            parts.append(f"<b>{fname}:</b> {fval}")
        elif fval:
            parts.append(fval)
    if embed.footer and embed.footer.text:
        parts.append(f"<i>{html.escape(str(embed.footer.text))}</i>")
    if embed.url:
        parts.append(html.escape(str(embed.url)))
    return "\n".join(p for p in parts if p)


class DiscordListener:
    """Reads messages from a set of Discord channels and queues them.

    Intentionally does NOT filter by source bot name, because the Potion
    server has both bot-driven channels (Perp Bot Calls) and human-driven
    channels (Manual Perp Calls, Prediction Calls). The router decides what
    to do with each message based on the source channel ID.

    Args:
        bot_token: Discord bot authentication token.
        monitored_channel_ids: Set of channel IDs to listen on.
        queue: Outgoing async queue of IncomingMessage objects.
    """

    def __init__(
        self,
        bot_token: str,
        monitored_channel_ids: set[int],
        queue: asyncio.Queue[IncomingMessage],
        activity_hook=None,
        activity_channel_ids: set[int] | None = None,
    ):
        """
        Args:
            activity_hook: optional async callable
                ``async def (discord_user_id: str, channel_id: int)``
                invoked for every message whose channel is in
                ``activity_channel_ids``. Used by the retention automations
                to record per-user post timestamps.
            activity_channel_ids: set of channel IDs to record activity for.
                Independent from ``monitored_channel_ids`` (signals). Empty
                or None disables the hook.
        """
        self._bot_token = bot_token
        self._monitored = set(monitored_channel_ids)
        self._queue = queue
        self._activity_hook = activity_hook
        self._activity_channels = set(activity_channel_ids or set())

        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        # Server Members privileged intent: required for on_member_update
        # (cancel-survey DM watcher needs it). Must also be enabled in the
        # Discord developer portal under Bot -> Privileged Gateway Intents.
        intents.members = True
        self._client = discord.Client(intents=intents)

        self._setup_handlers()

    @property
    def client(self) -> discord.Client:
        """Expose the underlying discord.Client so other subsystems (slash
        commands, presence updates) can attach to the same connection."""
        return self._client

    def _setup_handlers(self) -> None:
        client = self._client

        @client.event
        async def on_ready():
            logger.info(
                "Discord listener connected as %s — watching %d channel(s): %s",
                client.user,
                len(self._monitored),
                sorted(self._monitored),
            )

        @client.event
        async def on_message(message: discord.Message):
            # Skip our own messages.
            if message.author == client.user:
                return

            channel_id = message.channel.id

            # Activity hook fires for any tracked channel regardless of whether
            # the channel is a signal source. Ignore bot authors so we don't
            # record the Potion Perps Bot as an "active member".
            if (
                self._activity_hook is not None
                and channel_id in self._activity_channels
                and not getattr(message.author, "bot", False)
            ):
                try:
                    await self._activity_hook(
                        str(message.author.id), channel_id,
                    )
                except Exception:
                    logger.exception(
                        "activity_hook crashed for user=%s channel=%d",
                        message.author.id, channel_id,
                    )

            # Only queue for routing if this is a monitored signal channel.
            if channel_id not in self._monitored:
                return

            text = (message.content or "").strip()
            # If plain content is empty but the message carries embeds
            # (common for third-party alert bots like Onsight), serialize
                        # the first embed into text so the router has something to
            # forward. Multiple embeds are concatenated with a blank line.
            if not text and message.embeds:
                embed_blocks = [
                    _serialize_embed(e) for e in message.embeds if e is not None
                ]
                text = "\n\n".join(b for b in embed_blocks if b).strip()
            if not text:
                return

            channel_name = getattr(message.channel, "name", "") or str(channel_id)
            incoming = IncomingMessage(
                channel_id=channel_id,
                channel_name=channel_name,
                author_name=getattr(message.author, "display_name", str(message.author)),
                author_is_bot=bool(getattr(message.author, "bot", False)),
                content=text,
            )
            logger.debug(
                "Discord message captured: channel=%s author=%s len=%d",
                channel_name,
                incoming.author_name,
                len(text),
            )
            await self._queue.put(incoming)

    async def start(self) -> None:
        """Connect and run until cancelled or stopped."""
        logger.info("Starting Discord listener (channels=%s)", sorted(self._monitored))
        await self._client.start(self._bot_token)

    async def stop(self) -> None:
        logger.info("Stopping Discord listener")
        await self._client.close()
