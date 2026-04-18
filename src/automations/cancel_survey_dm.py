"""DM the exit survey AND enroll in winback on Elite role removal.

Single source of truth for "member cancelled": the Discord Elite role
transitioning from present to absent (set by Whop's automatic role sync).
When that fires we:

  1. DM the member a personalised exit-survey link (CANCEL_SURVEY_URL
     with ?member_id=X&username=Y&source=discord_role_removed appended)
  2. Look up their email (whop_members roster first, verified_users as
     fallback) and enroll them in the 3-email winback sequence

This replaces the older Whop cancellation-webhook flow entirely, so
WHOP_WEBHOOK_SECRET is no longer required.

State is tracked in SQLite (``data/cancel_survey_dms.db``) to prevent
double-DMing on role flickers (admin removes-then-re-adds within minutes,
or the bot reconnects and re-emits stale member_update events). The same
dedupe prevents double-enrolling in the email sequence.

Requires the ``Server Members`` privileged intent enabled in the Discord
developer portal AND ``intents.members = True`` on the discord.Client. If
the intent is missing the listener registers but never fires.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

import aiosqlite
import discord

if TYPE_CHECKING:
    from src.automations.whop_members_db import WhopMembersDB
    from src.email_bot.db import EmailDB
    from src.verification.db import VerificationDB

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
        rejoin_url: str = "https://whop.com/potion",
        whop_members_db: "WhopMembersDB | None" = None,
        verification_db: "VerificationDB | None" = None,
        email_db: "EmailDB | None" = None,
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
        self._rejoin_url = rejoin_url
        self._whop_members_db = whop_members_db
        self._verification_db = verification_db
        self._email_db = email_db
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
        embed = self._build_embed(after, url)

        try:
            channel = await after.create_dm()
            await channel.send(embed=embed)
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

        # Enroll in winback email sequence. Runs regardless of whether the
        # survey DM landed (they still cancelled; the email is our second
        # touch). exit_reason starts as "other" and can be updated later via
        # the survey response webhook once that's wired up.
        await self._enroll_in_winback(user_id, after.name)

    def _build_url(self, member: discord.Member) -> str:
        params = (
            "?type=exit"
            f"&member_id={quote_plus(str(member.id))}"
            f"&username={quote_plus(member.name)}"
            "&source=discord_role_removed"
        )
        return self._survey_url + "/" + params

    # Hosted on the Netlify site alongside the survey form. Bot embeds pull it
    # in as the thumbnail. Same file also acts as the site favicon + OG image,
    # so upload once at potion-feedback.netlify.app/potion-logo.png.
    _LOGO_URL = "https://potion-feedback.netlify.app/potion-logo.png"
    _POTION_PURPLE = 0x6b4fbb

    def _build_embed(self, member: discord.Member, url: str) -> discord.Embed:
        """Rich Discord embed for the exit survey DM.

        Uses the Potion logo as thumbnail so the DM looks branded, not spammy.
        The title links directly to the survey so a single click / tap on
        the embed header takes the user there. Description keeps the same
        copy as the old plaintext version for consistency.
        """
        name = member.display_name or member.name
        embed = discord.Embed(
            title="Sorry to see you go",
            url=url,
            description=(
                f"Hey {name},\n\n"
                "Your Potion access just ended. Before you go, we'd love a "
                "quick line on what didn't work. Takes 20 seconds and the "
                "team reads every reply.\n\n"
                f"**[Open the survey]({url})**\n\n"
                "Whatever you share goes straight to us. No follow-up sales "
                "pitch."
            ),
            color=self._POTION_PURPLE,
        )
        embed.set_thumbnail(url=self._LOGO_URL)
        embed.set_footer(text="Potion Alpha Team")
        return embed

    async def _enroll_in_winback(self, discord_user_id: str, username: str) -> None:
        """Look up email for this Discord user and enroll in winback sequence.

        Source priority:
          1. whop_members (full Elite roster, 126k members) — covers every
             paying member whether or not they've verified on Telegram
          2. verified_users (Telegram-verified subset) — fallback for
             members whose Discord isn't linked in Whop

        Silent skip if:
          - email_db is None (automations disabled)
          - no email source is wired
          - discord_user_id not found in either source
          - the email is blank

        All error handling is defensive: cancellation tracking should never
        crash the on_member_update listener.
        """
        if self._email_db is None:
            return

        email = ""
        try:
            if self._whop_members_db is not None:
                member = await self._whop_members_db.get_by_discord(discord_user_id)
                if member is not None and member.email:
                    email = member.email
            if not email and self._verification_db is not None:
                verified_users = await self._verification_db.list_active()
                for user in verified_users:
                    if user.discord_user_id == discord_user_id and user.email:
                        email = user.email
                        break
        except Exception:
            logger.exception(
                "CancelSurveyDM email lookup failed for discord=%s",
                discord_user_id,
            )
            return

        if not email:
            logger.info(
                "CancelSurveyDM: no email on file for discord=%s (%s), "
                "skipping winback enroll",
                discord_user_id, username,
            )
            return

        try:
            from src.email_bot.db import Subscriber  # local to avoid cycles

            sub = Subscriber(
                email=email,
                name=username,
                trigger_type="cancellation",
                exit_reason="other",  # updated later via survey response webhook
                rejoin_url=self._rejoin_url,
                created_at=int(time.time()),
            )
            await self._email_db.upsert_subscriber(sub)
            send_ids = await self._email_db.schedule_sequence(
                email=email, sequence="winback",
            )
            logger.info(
                "CancelSurveyDM: enrolled %s (%s) in winback sequence, "
                "%d emails queued",
                email, username, len(send_ids),
            )
        except Exception:
            logger.exception(
                "CancelSurveyDM winback enroll failed for %s", email,
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
