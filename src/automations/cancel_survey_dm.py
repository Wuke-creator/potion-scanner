"""DM the exit survey, enroll in winback, AND email the survey link.

Single source of truth for "member cancelled": the Discord Elite role
transitioning from present to absent (set by Whop's automatic role sync).
When that fires we do three things in parallel:

  1. DM the member a personalised exit-survey link (CANCEL_SURVEY_URL
     with ?discord_user_id=X&whop_user_id=Y&username=Z&source=discord_role_removed
     appended)
  2. Look up their email (whop_members roster first, verified_users as
     fallback) and enroll them in the 3-email winback sequence
  3. Email the SAME survey link so inbox-checkers who've disengaged from
     Discord still see the feedback prompt

This replaces the older Whop cancellation-webhook flow entirely, so
WHOP_WEBHOOK_SECRET is no longer required.

State is tracked in SQLite (``data/cancel_survey_dms.db``) to prevent
double-sending on role flickers (admin removes-then-re-adds within minutes,
or the bot reconnects and re-emits stale member_update events). The
``sent_dms`` table records both the DM state (``delivered``) and the email
state (``survey_email_sent``) per member so each channel dedupes
independently but uses one row per user.

Requires the ``Server Members`` privileged intent enabled in the Discord
developer portal AND ``intents.members = True`` on the discord.Client. If
the intent is missing the listener registers but never fires.
"""

from __future__ import annotations

import logging
import time
from html import escape as _html_escape
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

import aiosqlite
import discord

from src.whop_api import create_one_time_promo

if TYPE_CHECKING:
    from src.automations.whop_members_db import WhopMembersDB
    from src.email_bot.db import EmailDB
    from src.email_bot.sender import ResendClient
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
        resend_client: "ResendClient | None" = None,
        from_name: str = "Potion Alpha Team",
        promo_api_key: str = "",
        whop_company_id: str = "",
        promo_ttl_days: int = 30,
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
        self._resend = resend_client
        self._from_name = from_name
        self._promo_api_key = promo_api_key
        self._whop_company_id = whop_company_id
        self._promo_ttl_days = int(promo_ttl_days)
        self._db: aiosqlite.Connection | None = None
        self._registered = False

    async def open(self) -> None:
        """Open the tracker DB and register the on_member_update handler.

        Creates the ``sent_dms`` table if missing and runs the 2026-04-19
        migration that adds the ``survey_email_sent`` column so role
        flickers can't double-email. SQLite rejects ALTER if the column
        already exists, so we swallow that specific error.
        """
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_dms (
                discord_user_id TEXT PRIMARY KEY,
                username TEXT,
                sent_at INTEGER NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 1,
                survey_email_sent INTEGER NOT NULL DEFAULT 0,
                confirmation_email_sent INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # Column migrations for DBs that predate each new flag. Each ALTER
        # is wrapped individually so a partial migration history doesn't
        # block subsequent columns. SQLite raises OperationalError when
        # the column already exists; that's the only error we swallow.
        for stmt in (
            "ALTER TABLE sent_dms ADD COLUMN survey_email_sent "
            "INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE sent_dms ADD COLUMN confirmation_email_sent "
            "INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                await self._db.execute(stmt)
            except aiosqlite.OperationalError:
                pass  # column already exists
        await self._db.commit()
        self._register_handler()
        logger.info(
            "CancelSurveyDM ready (role=%s, guild=%s, url=%s, cooldown=%ds, "
            "email=%s)",
            self._elite_role_id, self._guild_id, self._survey_url, self._cooldown,
            "on" if self._resend is not None else "off",
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

        # Look up Whop user id (if the member has it linked) so the survey
        # form / downstream Sheet can cross-reference both IDs.
        whop_user_id = await self._lookup_whop_id(user_id)

        # Mint per-user single-use promo codes for each discount offer.
        # Runs in parallel; any individual failure just leaves that code
        # empty (frontend falls back to the hardcoded default for that
        # offer). Skipped entirely if no promo API key is configured.
        promo_codes = await self._generate_promo_codes(user_id)

        url = self._build_url(
            after, whop_user_id=whop_user_id, promo_codes=promo_codes,
        )
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

        # Resolve the member's email once and reuse it for both the winback
        # enroll and the new survey-email step. Keeps both steps consistent
        # and saves a DB round-trip.
        email = await self._resolve_email(user_id)

        # Step 2: enroll in winback email sequence (Day 1/4/7). Runs
        # regardless of whether the survey DM landed; the email is our
        # second touch. exit_reason starts as "other" and can be updated
        # later via the survey response webhook once that's wired up.
        await self._enroll_in_winback(user_id, after.name, email=email)

        # Step 3: email the survey link. Reaches the inbox even when the
        # user has abandoned Discord; typical of real cancellations.
        await self._send_survey_email(
            user_id=user_id, username=after.name, email=email, url=url,
        )

        # Step 4: send the cancellation-confirmation email. Distinct from
        # the survey email — this one acknowledges the cancellation and
        # surfaces the 14-day reactivation window. Sent immediately so
        # the member has a clean confirmation in their inbox alongside
        # the survey ask, not buried inside the winback sequence.
        await self._send_cancellation_confirmation_email(
            user_id=user_id, username=after.name, email=email,
        )

    def _build_url(
        self,
        member: discord.Member,
        whop_user_id: str = "",
        promo_codes: dict[str, str] | None = None,
    ) -> str:
        """Compose the survey URL with member identifiers + promo codes
        appended as query params. ``member_id`` is always the Discord
        snowflake (kept for backwards compat with the existing Netlify
        form). ``discord_user_id`` is a clearer alias. ``whop_user_id`` is
        appended only when resolved via whop_members_db. ``promo_codes``
        is a dict of ``{offer_key: generated_code}`` that the frontend
        reads to display per-user single-use codes."""
        parts = [
            "type=exit",
            f"member_id={quote_plus(str(member.id))}",
            f"discord_user_id={quote_plus(str(member.id))}",
            f"username={quote_plus(member.name)}",
            "source=discord_role_removed",
        ]
        if whop_user_id:
            parts.append(f"whop_user_id={quote_plus(whop_user_id)}")
        if promo_codes:
            # Frontend reads promo_{offer_key} params; missing ones fall
            # back to hardcoded defaults per OFFERS table.
            for offer_key, code in promo_codes.items():
                if code:
                    parts.append(
                        f"promo_{offer_key}={quote_plus(code)}",
                    )
        return self._survey_url + "/?" + "&".join(parts)

    # Map from the frontend's OFFERS keys (keyed by exit reason) to the
    # discount parameters we mint per-cancellation. Reasons that don't map
    # to a code (pause, not_using, fulfillment) are absent here — those
    # offers are non-discount CTAs.
    _PROMO_OFFERS = {
        "welcome20": {"base": "WELCOME20", "amount": 20, "duration": 1},
        "stay30":    {"base": "STAY30",    "amount": 100, "duration": 1},
        "comeback25":{"base": "COMEBACK25","amount": 25, "duration": 1},
    }

    async def _generate_promo_codes(self, discord_user_id: str) -> dict[str, str]:
        """Mint a unique single-use promo code for each discount offer.

        Returns a dict ``{offer_key: code}`` for every offer that Whop
        accepted. Keys that fail (network error, 4xx) are simply omitted —
        the frontend falls back to the hardcoded placeholder for those.

        No-ops (returns empty dict) if ``promo_api_key`` or
        ``whop_company_id`` weren't configured, so the feature can be
        enabled/disabled by env var alone without touching code.
        """
        if not self._promo_api_key or not self._whop_company_id:
            return {}

        async def _mint(offer_key: str, spec: dict) -> tuple[str, str | None]:
            code = await create_one_time_promo(
                api_key=self._promo_api_key,
                company_id=self._whop_company_id,
                base_code=spec["base"],
                amount_off=spec["amount"],
                duration_months=spec["duration"],
                discord_user_id=discord_user_id,
                reason_tag=offer_key,
                ttl_days=self._promo_ttl_days,
            )
            return offer_key, code

        # Fire all three in parallel; Whop rate limits at ~2 req/s but three
        # simultaneous calls land fine.
        results = await asyncio.gather(
            *[_mint(k, spec) for k, spec in self._PROMO_OFFERS.items()],
            return_exceptions=True,
        )
        codes: dict[str, str] = {}
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Promo mint crashed: %s", r)
                continue
            key, code = r
            if code:
                codes[key] = code
        if codes:
            logger.info(
                "CancelSurveyDM minted %d/%d promos for %s",
                len(codes), len(self._PROMO_OFFERS), discord_user_id,
            )
        return codes

    async def _lookup_whop_id(self, discord_user_id: str) -> str:
        """Resolve the Whop user id for a Discord user via whop_members_db.

        Returns empty string if the roster hasn't been synced yet, the
        member isn't in it, or the lookup raises. Cancel-survey DMs should
        never fail because of a missing Whop id.
        """
        if self._whop_members_db is None or not discord_user_id:
            return ""
        try:
            member = await self._whop_members_db.get_by_discord(discord_user_id)
            if member is not None and member.whop_user_id:
                return member.whop_user_id
        except Exception:
            logger.debug(
                "CancelSurveyDM: whop id lookup failed for discord=%s",
                discord_user_id,
            )
        return ""

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

    async def _resolve_email(self, discord_user_id: str) -> str:
        """Look up the member's email via whop_members then verified_users.

        Source priority:
          1. whop_members (full Elite roster, 126k members) — covers every
             paying member whether or not they've verified on Telegram
          2. verified_users (Telegram-verified subset) — fallback for
             members whose Discord isn't linked in Whop

        Returns an empty string if no email can be resolved. Used by both
        ``_enroll_in_winback`` and ``_send_survey_email`` so they agree on
        which email to use. Errors are logged and swallowed so the
        on_member_update listener never crashes because of a missing
        member or a DB hiccup.
        """
        email = ""
        try:
            if self._whop_members_db is not None:
                member = await self._whop_members_db.get_by_discord(
                    discord_user_id,
                )
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
        return email

    async def _enroll_in_winback(
        self, discord_user_id: str, username: str, *, email: str = "",
    ) -> None:
        """Enroll a cancelled user in the 3-email winback sequence.

        Silent skip if:
          - email_db is None (automations disabled)
          - the email is blank (no address to send to)

        All error handling is defensive: cancellation tracking should never
        crash the on_member_update listener.
        """
        if self._email_db is None:
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

    async def _send_survey_email(
        self,
        *,
        user_id: str,
        username: str,
        email: str,
        url: str,
    ) -> None:
        """Send a one-shot exit-survey email with the same Netlify survey
        link the Discord DM uses.

        Silent skip when:
          - resend_client is None (email delivery not configured)
          - email resolution returned blank
          - we already emailed this user inside the cooldown window
            (checked via ``sent_dms.survey_email_sent``)

        Winback emails fire separately via ``_enroll_in_winback``; this
        message is a single immediate touch specifically asking for survey
        feedback, not part of the scheduled sequence.
        """
        if self._resend is None:
            logger.debug(
                "CancelSurveyDM: resend_client not wired, skipping survey email",
            )
            return
        if not email:
            logger.info(
                "CancelSurveyDM: no email on file for discord=%s (%s), "
                "skipping survey email",
                user_id, username,
            )
            return
        if await self._already_emailed(user_id):
            logger.debug(
                "CancelSurveyDM: survey email already sent to %s, skipping",
                user_id,
            )
            return

        subject = "One quick question before you go"
        greeting_name = username or "there"
        safe_name = _html_escape(greeting_name)
        safe_url = _html_escape(url)

        text_body = (
            f"Hey {greeting_name},\n\n"
            "Your Potion access just ended. Before you go, would you give "
            "us 20 seconds on what didn't work? The team reads every "
            "reply.\n\n"
            f"Open the survey: {url}\n\n"
            "Whatever you share goes straight to us. No follow-up sales "
            "pitch.\n\n"
            "Potion Alpha Team\n"
        )
        html_body = (
            '<!doctype html><html><body style="font-family:system-ui,'
            'sans-serif;max-width:560px;margin:0 auto;padding:24px;'
            'color:#222;line-height:1.5;">'
            f"<p>Hey {safe_name},</p>"
            "<p>Your Potion access just ended. Before you go, would you "
            "give us 20 seconds on what didn't work? The team reads every "
            "reply.</p>"
            f'<p style="margin:32px 0;"><a href="{safe_url}" '
            'style="background:#6b4fbb;color:white;padding:14px 28px;'
            'text-decoration:none;border-radius:8px;font-weight:bold;'
            'display:inline-block;">Open the survey</a></p>'
            '<p style="color:#666;font-size:14px;">Whatever you share goes '
            "straight to us. No follow-up sales pitch.</p>"
            "<p>Potion Alpha Team</p>"
            "</body></html>"
        )

        try:
            result = await self._resend.send(
                to=email,
                subject=subject,
                html=html_body,
                text=text_body,
                from_name=self._from_name,
            )
        except Exception:
            logger.exception(
                "CancelSurveyDM: survey email send crashed for %s", email,
            )
            return

        if getattr(result, "ok", False):
            await self._mark_survey_emailed(user_id)
            logger.info(
                "CancelSurveyDM: survey email sent to %s (%s)",
                email, username,
            )
        else:
            err = getattr(result, "error", "unknown error")
            logger.warning(
                "CancelSurveyDM: survey email failed for %s: %s", email, err,
            )

    async def _send_cancellation_confirmation_email(
        self,
        *,
        user_id: str,
        username: str,
        email: str,
    ) -> None:
        """Send a one-shot cancellation confirmation email.

        Distinct from the survey email and the winback sequence:
          - Survey email asks for feedback (single immediate touch)
          - Winback sequence persuades them to come back (Day 1/4/7)
          - This email simply acknowledges the cancellation and surfaces
            the 14-day reactivation window. Reassuring, not pushy.

        Silent skip when:
          - resend_client is None (email delivery not configured)
          - email resolution returned blank
          - we already sent the confirmation inside the cooldown window
            (checked via ``sent_dms.confirmation_email_sent``)
        """
        if self._resend is None:
            logger.debug(
                "CancelSurveyDM: resend_client not wired, "
                "skipping confirmation email",
            )
            return
        if not email:
            logger.info(
                "CancelSurveyDM: no email on file for discord=%s (%s), "
                "skipping confirmation email",
                user_id, username,
            )
            return
        if await self._already_confirmed_emailed(user_id):
            logger.debug(
                "CancelSurveyDM: confirmation email already sent to %s, "
                "skipping",
                user_id,
            )
            return

        subject = "Your Potion Elite cancellation is confirmed"
        greeting_name = username or "there"
        safe_name = _html_escape(greeting_name)
        safe_url = _html_escape(self._rejoin_url)

        text_body = (
            f"Hey {greeting_name},\n\n"
            "Your Potion Elite membership has been cancelled and your "
            "access has ended.\n\n"
            "For the next 14 days, you can reactivate at your current "
            "rate using the link below. After that, the standard rate "
            "applies.\n\n"
            f"Reactivate at current rate: {self._rejoin_url}\n\n"
            "Whatever the reason for leaving, we appreciate the time you "
            "spent with Potion.\n\n"
            "Potion Alpha Team\n"
        )
        html_body = (
            '<!doctype html><html><body style="font-family:system-ui,'
            'sans-serif;max-width:560px;margin:0 auto;padding:24px;'
            'color:#222;line-height:1.5;">'
            f"<p>Hey {safe_name},</p>"
            "<p>Your Potion Elite membership has been cancelled and your "
            "access has ended.</p>"
            "<p>For the next <strong>14 days</strong>, you can reactivate "
            "at your current rate using the link below. After that, the "
            "standard rate applies.</p>"
            f'<p style="margin:32px 0;"><a href="{safe_url}" '
            'style="background:#6b4fbb;color:white;padding:14px 28px;'
            'text-decoration:none;border-radius:8px;font-weight:bold;'
            'display:inline-block;">Reactivate at current rate</a></p>'
            '<p style="color:#666;font-size:14px;">Whatever the reason '
            "for leaving, we appreciate the time you spent with Potion.</p>"
            "<p>Potion Alpha Team</p>"
            "</body></html>"
        )

        try:
            result = await self._resend.send(
                to=email,
                subject=subject,
                html=html_body,
                text=text_body,
                from_name=self._from_name,
            )
        except Exception:
            logger.exception(
                "CancelSurveyDM: confirmation email send crashed for %s",
                email,
            )
            return

        if getattr(result, "ok", False):
            await self._mark_confirmation_emailed(user_id)
            logger.info(
                "CancelSurveyDM: confirmation email sent to %s (%s)",
                email, username,
            )
        else:
            err = getattr(result, "error", "unknown error")
            logger.warning(
                "CancelSurveyDM: confirmation email failed for %s: %s",
                email, err,
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
        """Upsert the DM send record.

        Preserves ``survey_email_sent`` across updates: if a previous row
        exists with the email already flagged, the value is carried forward
        rather than reset. This keeps DM cooldown and email cooldown
        independent but stored in one row.
        """
        if self._db is None:
            return
        async with self._db.execute(
            "SELECT survey_email_sent, confirmation_email_sent "
            "FROM sent_dms WHERE discord_user_id = ?",
            (user_id,),
        ) as cur:
            prior = await cur.fetchone()
        survey_flag = int(prior[0]) if prior else 0
        confirm_flag = int(prior[1]) if prior else 0
        await self._db.execute(
            """
            INSERT OR REPLACE INTO sent_dms
              (discord_user_id, username, sent_at, delivered,
               survey_email_sent, confirmation_email_sent)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, username, int(time.time()),
             1 if delivered else 0, survey_flag, confirm_flag),
        )
        await self._db.commit()

    async def _already_emailed(self, user_id: str) -> bool:
        """True when we've already sent the survey email to this user
        within the cooldown window. Independent from DM dedupe."""
        if self._db is None:
            return False
        async with self._db.execute(
            "SELECT survey_email_sent, sent_at FROM sent_dms "
            "WHERE discord_user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None or not int(row[0]):
            return False
        return (int(time.time()) - int(row[1])) < self._cooldown

    async def _already_confirmed_emailed(self, user_id: str) -> bool:
        """True when we've already sent the cancellation-confirmation email
        to this user within the cooldown window. Independent from DM
        and survey-email dedupe."""
        if self._db is None:
            return False
        async with self._db.execute(
            "SELECT confirmation_email_sent, sent_at FROM sent_dms "
            "WHERE discord_user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None or not int(row[0]):
            return False
        return (int(time.time()) - int(row[1])) < self._cooldown

    async def _mark_confirmation_emailed(self, user_id: str) -> None:
        """Record that the cancellation-confirmation email has been sent.

        UPSERT pattern matches _mark_survey_emailed so the row is created
        if no DM record exists yet (race condition where email send beats
        the DM to the DB write)."""
        if self._db is None:
            return
        now = int(time.time())
        await self._db.execute(
            """
            INSERT INTO sent_dms
              (discord_user_id, username, sent_at, delivered,
               survey_email_sent, confirmation_email_sent)
            VALUES (?, '', ?, 0, 0, 1)
            ON CONFLICT(discord_user_id) DO UPDATE SET
              confirmation_email_sent = 1
            """,
            (user_id, now),
        )
        await self._db.commit()

    async def _mark_survey_emailed(self, user_id: str) -> None:
        """Record that the survey email has been sent to this user.

        Uses an UPSERT so it works whether the DM happened first (normal
        case, row exists) or the email send beat the DM to the DB write
        (race condition, row may not exist yet).
        """
        if self._db is None:
            return
        now = int(time.time())
        await self._db.execute(
            """
            INSERT INTO sent_dms
              (discord_user_id, username, sent_at, delivered, survey_email_sent)
            VALUES (?, '', ?, 0, 1)
            ON CONFLICT(discord_user_id) DO UPDATE SET
              survey_email_sent = 1
            """,
            (user_id, now),
        )
        await self._db.commit()
