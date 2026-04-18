"""DM the exit survey to members who lose the Elite role.

Subscribes to ``discord.Client.on_member_update`` and watches for the Elite
role transitioning from present to absent. When that fires, the cancelled
member is DM'd a personalised link to the cancellation feedback survey
hosted at ``CANCEL_SURVEY_URL``. Discord ID + username are appended to the
URL so submissions can be cross-referenced back to who left.

State is tracked in SQLite (``data/cancel_survey_dms.db``) to prevent
double-DMing on role flickers (admin removes-then-re-adds within minutes,
or the bot reconnects and re-emits stale member_update events).

Requires the ``Server Members`` privileged intent enabled in the Discord
developer portal AND ``intents.members = True`` on the discord.Client. If
the intent is missing the listener registers but never fires.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import quote_plus

import aiosqlite
import discord

logger = logging.getLogger(__name__)


class CancelSurveyDM:
    """Watch Elite role removals and DM the exit survey link.

    Args:
        client: The shared discord.Client (from DiscordListener.client).
        elite_role_id: The Elite role's snowflake. Pulled from
            ``DISCORD_ELITE_ROLE_ID`` env var via DiscordOAuthConfig.
        guild_id: Restrict to this guild (Potion). Set 0 to allow any guild
            the bot is in. The bot is normally only in one guild anyway.
        survey_url: Base URL of the deployed survey. We append
            ``?type=exit&member_id=...&username=...&source=discord_role_removed``.
        db_path: SQLite file for the sent_dms tracker.
        cooldown_seconds: Don't re-DM the same user within this window. Default
            7 days — enough that a brief role flicker won't double-DM, while
            still catching anyone who actually re-cancels later.
    """

    def __init__(
        self,
        *,
        client: discord.Client,
        elite_role_id: int,
        guild_id: int,
        survey_url: str,
        db_path: str = "data/cancel_survey_dms.db",
        cooldown_seconds: int = 7 * 24 * 60 * 60,
    ):
        if elite_role_id <= 0:
            raise ValueError("elite_role_id required and must be > 0")
        if not survey_url:
            raise ValueError("survey_url required")
        self._client = client
        self._elite_role_id = int(elite_role_id)
        self._guild_id = int(guild_id) if guild_id else 0
        self._survey_url = survey_url.rstrip("/")
        self._db_path = db_path
        self._cooldown = int(cooldown_seconds)
        self._db: aiosqlite.Connection | None = None
        self._registered = False

    async def open(self) -> None:
        """Open the tracker DB and register the on_member_update handler."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_dms (
                discord_user_id TEXT PRIMARY KEY,
                username TEXT,
                sent_at INTEGER NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        await self._db.commit()
        self._register_handler()
        logger.info(
            "CancelSurveyDM ready (role=%s, guild=%s, url=%s, cooldown=%ds)",
            self._elite_role_id, self._guild_id, self._survey_url, self._cooldown,
        )

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _register_handler(self) -> None:
        if self._registered:
            return
        self._registered = True

        @self._client.event
        async def on_member_update(before: discord.Member, after: discord.Member):
            try:
                await self._handle_update(before, after)
            except Exception:
                logger.exception(
                    "CancelSurveyDM crashed for user=%s",
                    getattr(after, "id", "?"),
                )

    async def _handle_update(
        self, before: discord.Member, after: discord.Member,
    ) -> None:
        if self._guild_id and after.guild.id != self._guild_id:
            return

        had = any(r.id == self._elite_role_id for r in before.roles)
        has = any(r.id == self._elite_role_id for r in after.roles)
        if not (had and not has):
            return

        # Don't DM bots, ourselves, or admins (admin role removals are usually
        # internal moves, not real cancellations).
        if after.bot:
            return

        user_id = str(after.id)
        if await self._already_sent_recently(user_id):
            logger.debug("CancelSurveyDM skip %s — within cooldown", user_id)
            return

        url = self._build_url(after)
        message = self._build_message(after, url)

        try:
            channel = await after.create_dm()
            await channel.send(message)
            await self._record_sent(user_id, after.name, delivered=True)
            logger.info(
                "CancelSurveyDM sent to %s (%s) — survey url logged in DM",
                user_id, after.name,
            )
        except discord.Forbidden:
            # User has DMs from server members closed. Record so we don't
            # retry every time the role flickers.
            logger.info("CancelSurveyDM: DMs closed for %s (%s)", user_id, after.name)
            await self._record_sent(user_id, after.name, delivered=False)
        except discord.HTTPException as e:
            logger.warning(
                "CancelSurveyDM DM failed for %s (%s): %s",
                user_id, after.name, e,
            )

    def _build_url(self, member: discord.Member) -> str:
        params = (
            "?type=exit"
            f"&member_id={quote_plus(str(member.id))}"
            f"&username={quote_plus(member.name)}"
            "&source=discord_role_removed"
        )
        return self._survey_url + "/" + params

    def _build_message(self, member: discord.Member, url: str) -> str:
        name = member.display_name or member.name
        return (
            f"Hey {name},\n\n"
            "Your Potion access just ended. Before you go, we'd love a quick "
            "line on what didn't work. Takes 20 seconds and the team reads "
            "every reply.\n\n"
            f"{url}\n\n"
            "Whatever you share goes straight to us. No follow-up sales pitch."
        )

    async def _already_sent_recently(self, user_id: str) -> bool:
        if self._db is None:
            return False
        async with self._db.execute(
            "SELECT sent_at FROM sent_dms WHERE discord_user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False
        return (int(time.time()) - int(row[0])) < self._cooldown

    async def _record_sent(
        self, user_id: str, username: str, *, delivered: bool,
    ) -> None:
        if self._db is None:
            return
        await self._db.execute(
            """
            INSERT OR REPLACE INTO sent_dms
              (discord_user_id, username, sent_at, delivered)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, username, int(time.time()), 1 if delivered else 0),
        )
        await self._db.commit()
