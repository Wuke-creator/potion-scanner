"""24h reverification cron — revokes users who lost the Elite Discord role.

Runs as a background asyncio task. Each cycle:

  1. List all active verified_users from the DB
  2. For each user:
     a. Decrypt the stored Discord refresh token
     b. POST /oauth2/token (refresh_token grant) to get a new access token
        and rotated refresh token
     c. Call /users/@me/guilds/{POTION_GUILD_ID}/member, check for the
        Elite role in the user's roles array
     d. If still has role: persist the new refresh token, update
        last_checked_at
     e. If role removed (or user left the guild, or refresh fails): mark
        inactive, DM the user a notice with the Elite signup URL. The
        dispatcher will then skip them on every future alert.
  3. Sleep ``reverify_interval_seconds``

The job never raises — every user is wrapped in a try/except so one bad
account can't stall the whole loop. Sleeps between users to respect
Discord rate limits.
"""

from __future__ import annotations

import asyncio
import logging

from cryptography.fernet import Fernet, InvalidToken
from telegram import Bot
from telegram.error import TelegramError

from src.config import Config
from src.verification.db import VerificationDB, VerifiedUser
from src.verification.discord_oauth import (
    DiscordNotInGuildError,
    DiscordOAuthClient,
    DiscordOAuthError,
)

logger = logging.getLogger(__name__)


class ReverifyJob:
    """Periodic re-verification of every active user against Discord."""

    def __init__(
        self,
        config: Config,
        db: VerificationDB,
        oauth_client: DiscordOAuthClient,
        fernet: Fernet,
        telegram_bot: Bot,
    ):
        self._config = config
        self._db = db
        self._oauth = oauth_client
        self._fernet = fernet
        self._bot = telegram_bot
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="reverify_job")
        logger.info(
            "Reverify job started (interval=%ds)",
            self._config.verification.reverify_interval_seconds,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None
        logger.info("Reverify job stopped")

    async def _run(self) -> None:
        interval = self._config.verification.reverify_interval_seconds
        while not self._stop_event.is_set():
            try:
                await self._cycle()
            except Exception:
                logger.exception("Reverify cycle crashed; will retry next interval")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                return  # stop requested
            except asyncio.TimeoutError:
                continue

    async def _cycle(self) -> None:
        # Housekeeping: purge expired pending verification rows
        try:
            purged = await self._db.cleanup_expired_pending(
                max_age_seconds=self._config.verification.pending_ttl_seconds,
            )
            if purged:
                logger.info("Purged %d expired pending verification(s)", purged)
        except Exception:
            logger.exception("Failed to purge expired pending verifications")

        users = await self._db.list_active()
        logger.info("Reverify cycle: checking %d active user(s)", len(users))
        sleep_between = self._config.verification.reverify_sleep_between_users_ms / 1000.0
        revoked = 0
        for user in users:
            if self._stop_event.is_set():
                break
            try:
                still_active = await self._recheck_one(user)
                if not still_active:
                    revoked += 1
            except Exception:
                logger.exception(
                    "Reverify failed for telegram_user_id=%d", user.telegram_user_id,
                )
            await asyncio.sleep(sleep_between)
        logger.info("Reverify cycle complete: %d revoked", revoked)

    async def _recheck_one(self, user: VerifiedUser) -> bool:
        """Re-check one user against Discord. Returns True if still Elite."""
        try:
            refresh_token = self._fernet.decrypt(
                user.refresh_token_encrypted.encode()
            ).decode()
        except InvalidToken:
            logger.error(
                "Invalid Fernet token for telegram_user_id=%d — revoking",
                user.telegram_user_id,
            )
            await self._revoke(user, mark_inactive=True)
            return False

        try:
            tokens = await self._oauth.refresh_access_token(refresh_token)
        except DiscordOAuthError as e:
            logger.warning(
                "Discord refresh failed for telegram_user_id=%d: %s — revoking",
                user.telegram_user_id, e,
            )
            await self._revoke(user, mark_inactive=True)
            return False

        try:
            elite_member = await self._oauth.check_elite_role(
                access_token=tokens.access_token,
                guild_id=self._config.discord.guild_id,
            )
        except DiscordNotInGuildError:
            logger.info(
                "User %d left the Potion guild — revoking",
                user.telegram_user_id,
            )
            await self._revoke(user, mark_inactive=True)
            return False
        except DiscordOAuthError as e:
            logger.warning(
                "Discord member check failed for telegram_user_id=%d: %s — leaving as-is",
                user.telegram_user_id, e,
            )
            # Don't revoke on transient API errors. Try again next cycle.
            return True

        if elite_member is None:
            logger.info(
                "User %d no longer holds the Elite role — revoking",
                user.telegram_user_id,
            )
            await self._revoke(user, mark_inactive=True)
            return False

        # Still Elite — persist the rotated refresh token
        new_encrypted = self._fernet.encrypt(tokens.refresh_token.encode()).decode()
        await self._db.update_after_recheck(
            telegram_user_id=user.telegram_user_id,
            is_active=True,
            new_refresh_token_encrypted=new_encrypted,
        )
        return True

    async def _revoke(self, user: VerifiedUser, mark_inactive: bool) -> None:
        """Revoke access: flip is_active off + DM the user a notice.

        The dispatcher filters on ``is_active = 1`` so setting it to 0 is
        enough to stop all future DMs.
        """
        if mark_inactive:
            await self._db.update_after_recheck(
                telegram_user_id=user.telegram_user_id, is_active=False,
            )
        signup_url = (
            self._config.discord_oauth.elite_signup_url
            or "https://whop.com/potion"
        )
        try:
            await self._bot.send_message(
                chat_id=user.telegram_user_id,
                text=(
                    "Your Elite role in the Potion Discord is no longer "
                    "present, so signal access has been revoked.\n\n"
                    f"Re-activate Elite: {signup_url}\n\n"
                    "Once you have the role back, run /verify to start "
                    "receiving calls again."
                ),
            )
        except TelegramError as e:
            logger.debug(
                "Could not DM lapse notice to telegram_user_id=%d: %s",
                user.telegram_user_id, e,
            )
