"""Lifecycle handle for the entire verification subsystem.

Wraps the four pieces (DB, Discord OAuth client, OAuth callback server,
Telegram command handlers, reverify job) into a single object that
main.py can start and stop with two calls.
"""

from __future__ import annotations

import logging

import aiohttp
from cryptography.fernet import Fernet
from telegram import Bot
from telegram.ext import Application, ApplicationBuilder

from src.analytics import AnalyticsDB
from src.config import Config
from src.verification.commands import VerificationCommands
from src.verification.db import VerificationDB
from src.verification.discord_oauth import DiscordOAuthClient
from src.verification.oauth_callback import OAuthCallbackServer
from src.verification.reverify_job import ReverifyJob

logger = logging.getLogger(__name__)


class VerificationRuntime:
    """All verification components, started and stopped together."""

    def __init__(
        self,
        config: Config,
        db: VerificationDB,
        http_session: aiohttp.ClientSession,
        oauth_client: DiscordOAuthClient,
        fernet: Fernet,
        telegram_bot: Bot,
        telegram_application: Application,
        commands: VerificationCommands,
        callback_server: OAuthCallbackServer,
        reverify_job: ReverifyJob,
    ):
        self._config = config
        self._db = db
        self._http_session = http_session
        self._oauth = oauth_client
        self._fernet = fernet
        self._telegram_bot = telegram_bot
        self._telegram_application = telegram_application
        self._commands = commands
        self._callback_server = callback_server
        self._reverify_job = reverify_job
        self._started = False

    @property
    def db(self) -> VerificationDB:
        """The shared verification DB. The Dispatcher also reads from this."""
        return self._db

    @property
    def callback_server(self) -> OAuthCallbackServer:
        """Expose the OAuth callback server so other subsystems can mount
        extra aiohttp routes on its shared app (port 8080)."""
        return self._callback_server

    async def start(self) -> None:
        if self._started:
            return
        self._commands.register(self._telegram_application)
        await self._telegram_application.initialize()
        await self._telegram_application.start()
        if self._telegram_application.updater is not None:
            await self._telegram_application.updater.start_polling()
        await self._callback_server.start()
        await self._reverify_job.start()
        self._started = True
        logger.info("VerificationRuntime started")

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        try:
            await self._reverify_job.stop()
        except Exception:
            logger.exception("reverify_job stop failed")
        try:
            await self._callback_server.stop()
        except Exception:
            logger.exception("callback_server stop failed")
        try:
            if self._telegram_application.updater is not None:
                await self._telegram_application.updater.stop()
            await self._telegram_application.stop()
            await self._telegram_application.shutdown()
        except Exception:
            logger.exception("telegram application stop failed")
        try:
            await self._oauth.close()
        except Exception:
            logger.exception("discord oauth client close failed")
        try:
            await self._http_session.close()
        except Exception:
            logger.exception("http session close failed")
        try:
            await self._db.close()
        except Exception:
            logger.exception("db close failed")
        logger.info("VerificationRuntime stopped")


async def build_verification_runtime(
    config: Config,
    telegram_bot: Bot,
    analytics: AnalyticsDB | None = None,
) -> VerificationRuntime:
    """Construct and wire the entire verification subsystem.

    The caller is responsible for calling ``.start()`` and ``.stop()``.
    ``analytics`` is optional; when provided it powers the /data command.
    """
    db = VerificationDB(db_path=config.verification.db_path)
    await db.open()

    fernet = Fernet(config.oauth.refresh_token_encryption_key.encode())

    http_session = aiohttp.ClientSession()
    oauth_client = DiscordOAuthClient(config.discord_oauth, session=http_session)

    callback_server = OAuthCallbackServer(
        config=config,
        db=db,
        oauth_client=oauth_client,
        fernet=fernet,
        telegram_bot=telegram_bot,
    )

    # python-telegram-bot v22 requires its own Application for command handling.
    telegram_application = (
        ApplicationBuilder().token(config.telegram.bot_token).build()
    )
    commands = VerificationCommands(
        config=config, db=db, oauth_client=oauth_client, analytics=analytics,
    )

    reverify_job = ReverifyJob(
        config=config,
        db=db,
        oauth_client=oauth_client,
        fernet=fernet,
        telegram_bot=telegram_bot,
    )

    return VerificationRuntime(
        config=config,
        db=db,
        http_session=http_session,
        oauth_client=oauth_client,
        fernet=fernet,
        telegram_bot=telegram_bot,
        telegram_application=telegram_application,
        commands=commands,
        callback_server=callback_server,
        reverify_job=reverify_job,
    )
