"""aiohttp web server hosting the Discord OAuth callback.

The flow (DM-based, Discord-gated):

  1. Browser redirects to ``GET /oauth/discord/callback?code=...&state=...``
  2. We verify the state token (HMAC + TTL) → telegram_user_id
  3. We consume the matching ``pending_verifications`` row → code_verifier
  4. We exchange the code for ``(access_token, refresh_token)`` against
     Discord
  5. We call ``/users/@me/guilds/{POTION_GUILD_ID}/member`` and look for
     the Elite role in the user's role list
  6. On success: encrypt the refresh token, persist a verified_users row,
     DM the user a welcome message — they will now receive every trading
     call as a direct message automatically
  7. On failure (no role, or not in the Potion guild): DM the user a
     denial with the Elite signup URL

The handler always returns a small HTML page so the browser shows a clear
result. Errors are logged with full context but never leak secrets.
"""

from __future__ import annotations

import logging

from aiohttp import web
from cryptography.fernet import Fernet
from telegram import Bot
from telegram.error import TelegramError

from src.config import Config
from src.verification.db import VerificationDB
from src.verification.commands import send_channel_picker
from src.verification.discord_oauth import (
    DiscordNotInGuildError,
    DiscordOAuthClient,
    DiscordOAuthError,
)
from src.verification.state_token import StateTokenError, verify as verify_state

logger = logging.getLogger(__name__)


_HTML_SUCCESS = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Verified</title></head>
<body style="font-family: system-ui; max-width: 480px; margin: 80px auto; text-align: center;">
  <h1>You're verified.</h1>
  <p>Check your Telegram DMs. You'll start receiving every Potion trading call automatically.</p>
</body>
</html>
"""

_HTML_DENIED = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>No Elite role</title></head>
<body style="font-family: system-ui; max-width: 480px; margin: 80px auto; text-align: center;">
  <h1>No Elite role found.</h1>
  <p>Unfortunately you are not able to access this service. You will need the Elite role in the Potion Discord. Check your Telegram DMs for the signup link.</p>
</body>
</html>
"""

_HTML_NOT_IN_GUILD = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Join the Potion Discord first</title></head>
<body style="font-family: system-ui; max-width: 480px; margin: 80px auto; text-align: center;">
  <h1>Join the Potion Discord first.</h1>
  <p>We couldn't find you in the Potion server. Join it, upgrade to Elite, then run /verify again.</p>
</body>
</html>
"""

_HTML_ERROR = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Verification error</title></head>
<body style="font-family: system-ui; max-width: 480px; margin: 80px auto; text-align: center;">
  <h1>Verification failed.</h1>
  <p>{message}</p>
  <p>Run /verify again on Telegram to start over.</p>
</body>
</html>
"""


class OAuthCallbackServer:
    """aiohttp server for the Discord OAuth callback."""

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
        self._app = web.Application()
        self._app.router.add_get(
            "/oauth/discord/callback", self._handle_callback,
        )
        self._app.router.add_get("/health", self._handle_health)
        self._runner: web.AppRunner | None = None
        self._site: web.BaseSite | None = None

    @property
    def app(self) -> web.Application:
        """Expose the aiohttp app so other modules can mount extra routes
        before ``start()`` is called (e.g. email bot webhooks)."""
        return self._app

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        host = self._config.oauth.host
        self._site = web.TCPSite(
            self._runner,
            host=host,
            port=self._config.oauth.port,
        )
        await self._site.start()
        logger.info(
            "OAuth callback server listening on %s:%d",
            host, self._config.oauth.port,
        )
        # Security note: binding to 0.0.0.0 exposes the OAuth callback and
        # webhook endpoints to any interface the host is reachable on. In
        # production the expected topology is cloudflared (or similar) in
        # front + a firewall blocking direct port access; if either fails,
        # this server is open to internet scanners. Warn loudly so the
        # operator doesn't miss that requirement.
        if host in ("0.0.0.0", "::", ""):
            logger.warning(
                "OAuth callback bound to %s (all interfaces). Production MUST "
                "firewall port %d from public access and front it with a "
                "tunnel / reverse proxy. Set oauth.host=127.0.0.1 in "
                "config.yaml if only a local tunnel needs to reach it.",
                host, self._config.oauth.port,
            )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
        logger.info("OAuth callback server stopped")

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def _handle_callback(self, request: web.Request) -> web.Response:
        code = request.query.get("code", "")
        state = request.query.get("state", "")
        error = request.query.get("error", "")

        if error:
            logger.warning("Discord OAuth callback received error: %s", error)
            return web.Response(
                text=_HTML_ERROR.format(message=f"Discord returned error: {error}"),
                content_type="text/html",
                status=400,
            )

        if not code or not state:
            return web.Response(
                text=_HTML_ERROR.format(message="Missing code or state."),
                content_type="text/html",
                status=400,
            )

        # Validate state signature + extract Telegram user ID
        try:
            telegram_user_id = verify_state(
                state, self._config.oauth.state_secret,
                max_age_seconds=self._config.verification.pending_ttl_seconds,
            )
        except StateTokenError as e:
            logger.warning("State token rejected: %s", e)
            return web.Response(
                text=_HTML_ERROR.format(message="Invalid or expired state token."),
                content_type="text/html",
                status=400,
            )

        # Consume the matching pending row to get the PKCE verifier
        pending = await self._db.consume_pending(state)
        if pending is None:
            logger.warning("No pending verification row for state token (replay?)")
            return web.Response(
                text=_HTML_ERROR.format(message="Verification expired or already used."),
                content_type="text/html",
                status=400,
            )
        if pending.telegram_user_id != telegram_user_id:
            logger.error(
                "State token user ID %d does not match pending row %d",
                telegram_user_id, pending.telegram_user_id,
            )
            return web.Response(
                text=_HTML_ERROR.format(message="State / pending mismatch."),
                content_type="text/html",
                status=400,
            )

        # Exchange code → tokens
        try:
            tokens = await self._oauth.exchange_code(
                code=code,
                code_verifier=pending.code_verifier,
                redirect_uri=self._config.oauth.redirect_uri,
            )
        except DiscordOAuthError as e:
            logger.error("Discord code exchange failed: %s", e)
            return web.Response(
                text=_HTML_ERROR.format(message="Could not exchange code with Discord."),
                content_type="text/html",
                status=502,
            )

        # Check Elite role in the Potion guild
        try:
            elite_member = await self._oauth.check_elite_role(
                access_token=tokens.access_token,
                guild_id=self._config.discord.guild_id,
            )
        except DiscordNotInGuildError:
            logger.info(
                "User %d authorized but is not in the Potion guild",
                telegram_user_id,
            )
            await self._dm_not_in_guild(telegram_user_id)
            return web.Response(text=_HTML_NOT_IN_GUILD, content_type="text/html")
        except DiscordOAuthError as e:
            logger.error("Discord member check failed: %s", e)
            return web.Response(
                text=_HTML_ERROR.format(message="Could not verify Discord role."),
                content_type="text/html",
                status=502,
            )

        if elite_member is None:
            await self._dm_denied(telegram_user_id)
            return web.Response(text=_HTML_DENIED, content_type="text/html")

        # Fetch email from Discord (requires `email` OAuth scope).
        # Failure is non-fatal; email-based features just skip users with no email.
        try:
            email = await self._oauth.fetch_email(tokens.access_token)
        except Exception:
            logger.exception("Email fetch failed (non-fatal)")
            email = ""

        # Persist
        try:
            encrypted_refresh = self._fernet.encrypt(
                tokens.refresh_token.encode()
            ).decode()
            await self._db.upsert_verified(
                telegram_user_id=telegram_user_id,
                discord_user_id=elite_member.discord_user_id,
                refresh_token_encrypted=encrypted_refresh,
                email=email,
            )
        except Exception:
            logger.exception("Failed to persist verified user")
            return web.Response(
                text=_HTML_ERROR.format(message="Could not save verification."),
                content_type="text/html",
                status=500,
            )

        try:
            await send_channel_picker(
                bot=self._bot,
                config=self._config,
                db=self._db,
                telegram_user_id=telegram_user_id,
            )
        except TelegramError:
            logger.exception(
                "Failed to DM channel picker to user %d", telegram_user_id,
            )
            return web.Response(
                text=_HTML_ERROR.format(
                    message="Verified, but couldn't DM you. "
                            "Make sure you've started a chat with the bot first, "
                            "then run /verify again.",
                ),
                content_type="text/html",
                status=500,
            )

        return web.Response(text=_HTML_SUCCESS, content_type="text/html")

    async def _dm_denied(self, telegram_user_id: int) -> None:
        signup_url = (
            self._config.discord_oauth.elite_signup_url
            or "https://whop.com/potion"
        )
        try:
            await self._bot.send_message(
                chat_id=telegram_user_id,
                text=(
                    "Unfortunately you are not able to access this service. "
                    "You will need the Elite role in the Potion Discord.\n\n"
                    f"Upgrade here: {signup_url}\n\n"
                    "Once you have Elite, run /verify again.\n\n"
                    "Need help? Head to Potion support:\n"
                    "https://discord.com/channels/1260259552763580537/1285628366162231346"
                ),
                disable_web_page_preview=False,
            )
        except TelegramError:
            logger.warning("Could not DM denial to user %d", telegram_user_id)

    async def _dm_not_in_guild(self, telegram_user_id: int) -> None:
        signup_url = (
            self._config.discord_oauth.elite_signup_url
            or "https://whop.com/potion"
        )
        try:
            await self._bot.send_message(
                chat_id=telegram_user_id,
                text=(
                    "We couldn't find you in the Potion Discord.\n\n"
                    f"Join first: {signup_url}\n\n"
                    "Once you're in and have the Elite role, run /verify again.\n\n"
                    "Need help? Head to Potion support:\n"
                    "https://discord.com/channels/1260259552763580537/1285628366162231346"
                ),
                disable_web_page_preview=False,
            )
        except TelegramError:
            logger.warning("Could not DM 'not in guild' to user %d", telegram_user_id)
