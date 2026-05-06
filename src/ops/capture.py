"""Ops capture hook.

Wires into the existing DiscordListener via an ``ops_hook`` callback.
For each message in a configured ops channel:

  1. Decide which "source" bucket the message belongs to (general / alpha
     / ticket-thread). Messages outside the configured set are dropped at
     the listener level and never reach this hook.
  2. Persist the raw message into ops.db.tickets (idempotent on message_id).
  3. Scan the message's mention list. For every mentioned user that is in
     the senior-staff set, insert a row into ops.db.leadership_mentions.
  4. If the author is themselves senior staff, increment their daily
     activity bucket. This keeps the StaffPerformance panel cheap to render.

Bots are skipped: we never log messages from bots (signal bots, ticket
bots) into the ops capture, only humans. Activity tracking is also bot-
scrubbed for the same reason.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord  # noqa: F401  (only used for type hints)

from src.ops.db import OpsDB

logger = logging.getLogger(__name__)


class OpsCapture:
    """Hook target for the DiscordListener ops_hook.

    Args:
        ops_db: opened OpsDB instance.
        general_channel_id: Potion #general chat ID.
        alpha_channel_id: Potion alpha chat ID.
        ticket_forum_id: Potion need-support forum channel ID. Threads
            under this forum are treated as tickets.
        senior_staff_ids: set of Discord user IDs (as strings) considered
            senior staff. Used for leadership @mention detection and
            activity tracking.
    """

    def __init__(
        self,
        ops_db: OpsDB,
        general_channel_id: int,
        alpha_channel_id: int,
        ticket_forum_id: int,
        senior_staff_ids: set[str],
    ):
        self._db = ops_db
        self._general = general_channel_id
        self._alpha = alpha_channel_id
        self._forum = ticket_forum_id
        self._staff = set(senior_staff_ids)

    def ops_channel_ids(self) -> set[int]:
        """Channel IDs the listener should treat as ops channels.

        For threads under the ticket forum, the channel ID at message-receive
        time is the THREAD ID (not the forum ID). The forum ID itself is only
        used as the parent_id check inside ``handle()``. So at listener
        registration time we only know about the two flat channels and have
        to opt-in all messages that have ``parent_id == ticket_forum_id``.
        See ``listener_should_capture()`` which is the proper gate.
        """
        return {self._general, self._alpha}

    def listener_should_capture(
        self,
        channel_id: int,
        parent_id: int | None,
    ) -> bool:
        """Listener-side gate. Returns True if this message is in scope.

        Threads under the configured ticket forum are in scope regardless
        of their thread channel ID, because thread IDs are auto-generated
        at thread creation time.
        """
        if channel_id == self._general or channel_id == self._alpha:
            return True
        if parent_id == self._forum:
            return True
        return False

    def _classify(self, channel_id: int, parent_id: int | None) -> str:
        if parent_id == self._forum:
            return "ticket"
        if channel_id == self._alpha:
            return "alpha"
        return "general"

    async def handle(self, message: "discord.Message") -> None:
        """Persist an in-scope, non-bot Discord message into ops.db.

        The DiscordListener filters bots out before calling this hook, but
        we double-check here so any caller that bypasses that filter still
        gets safe behavior.
        """
        try:
            if getattr(message.author, "bot", False):
                return

            channel = message.channel
            channel_id = channel.id
            parent_id = getattr(channel, "parent_id", None)
            if not self.listener_should_capture(channel_id, parent_id):
                return

            source = self._classify(channel_id, parent_id)
            channel_name = getattr(channel, "name", "") or str(channel_id)
            thread_name = channel_name if source == "ticket" else None

            author_id = str(message.author.id)
            author_name = getattr(message.author, "display_name", str(message.author))
            author_is_staff = author_id in self._staff
            content = (message.content or "").strip()
            created_at = int(message.created_at.timestamp()) if message.created_at else 0
            jump_url = getattr(message, "jump_url", "") or ""

            await self._db.record_ticket(
                message_id=message.id,
                channel_id=channel_id,
                channel_name=channel_name,
                parent_id=parent_id,
                thread_name=thread_name,
                source=source,
                author_id=author_id,
                author_name=author_name,
                author_is_staff=author_is_staff,
                content=content,
                created_at=created_at,
                jump_url=jump_url,
            )

            mentions = getattr(message, "mentions", None) or []
            for mention in mentions:
                mentioned_id = str(getattr(mention, "id", ""))
                if not mentioned_id or mentioned_id not in self._staff:
                    continue
                if mentioned_id == author_id:
                    # Self-mentions don't count as escalations.
                    continue
                await self._db.record_leadership_mention(
                    message_id=message.id,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    parent_id=parent_id,
                    thread_name=thread_name,
                    mentioned_id=mentioned_id,
                    mentioned_name=getattr(mention, "display_name", str(mention)),
                    author_id=author_id,
                    author_name=author_name,
                    content=content,
                    created_at=created_at,
                    jump_url=jump_url,
                )

            if author_is_staff:
                await self._db.bump_staff_activity(
                    staff_user_id=author_id,
                    channel_id=channel_id,
                    message_at=created_at or int(__import__("time").time()),
                )
        except Exception:
            logger.exception(
                "ops capture failed for message_id=%s channel_id=%s",
                getattr(message, "id", "?"),
                getattr(getattr(message, "channel", None), "id", "?"),
            )
