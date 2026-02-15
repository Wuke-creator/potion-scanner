"""Telegram bot — core class.

Wraps python-telegram-bot's Application to integrate with the
existing async main loop. Starts and stops cleanly alongside
the rest of the system.
"""

import logging

from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from src.config.settings import Config
from src.orchestrator import Orchestrator
from src.state.user_db import UserDatabase
from src.telegram.handlers.account import (
    account_nav_callback,
    balance_command,
    positions_command,
    status_command,
)
from src.telegram.handlers.approval import (
    close_trade_callback,
    confirm_close_callback,
    signal_approval_callback,
)
from src.telegram.handlers.admin import (
    generate_code_command,
    generate_codes_command,
    list_codes_command,
    revoke_code_command,
)
from src.telegram.handlers.config import (
    auto_command,
    config_callback,
    config_command,
    config_text_handler,
    preset_command,
)
from src.telegram.handlers.help import help_command, start_command
from src.telegram.handlers.registration import build_registration_handler
from src.telegram.handlers.trades import (
    history_command,
    stats_command,
    trade_detail_callback,
    trades_command,
)

logger = logging.getLogger(__name__)


class TelegramBot:
    """Manages the Telegram bot lifecycle."""

    def __init__(
        self,
        token: str,
        user_db: UserDatabase,
        config: Config,
        orchestrator: Orchestrator | None = None,
    ) -> None:
        self._token = token
        self._user_db = user_db
        self._config = config
        self._orchestrator = orchestrator
        self._app: Application | None = None

    async def start(self) -> None:
        """Build the application, register handlers, and start polling."""
        self._app = Application.builder().token(self._token).build()

        # Store shared dependencies in bot_data for handlers
        self._app.bot_data["user_db"] = self._user_db
        self._app.bot_data["config"] = self._config
        self._app.bot_data["admin_ids"] = self._config.telegram.admin_ids
        self._app.bot_data["orchestrator"] = self._orchestrator

        self._register_handlers()

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

        logger.info("Telegram bot started")

    @property
    def bot(self) -> "Bot":
        """Return the underlying telegram.Bot instance (available after start)."""
        if self._app is None:
            raise RuntimeError("TelegramBot not started yet")
        return self._app.bot

    async def stop(self) -> None:
        """Stop polling and shut down the application."""
        if self._app is None:
            return

        if self._app.updater and self._app.updater.running:
            await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()

        logger.info("Telegram bot stopped")

    def _register_handlers(self) -> None:
        """Register all command handlers."""
        self._app.add_handler(CommandHandler("start", start_command))
        self._app.add_handler(CommandHandler("help", help_command))

        # Registration conversation (must be before simple command handlers)
        self._app.add_handler(build_registration_handler())

        # Account monitoring
        self._app.add_handler(CommandHandler("balance", balance_command))
        self._app.add_handler(CommandHandler("positions", positions_command))
        self._app.add_handler(CommandHandler("status", status_command))
        self._app.add_handler(CallbackQueryHandler(account_nav_callback, pattern=r"^nav:"))

        # Configuration
        self._app.add_handler(CommandHandler("config", config_command))
        self._app.add_handler(CommandHandler("preset", preset_command))
        self._app.add_handler(CommandHandler("auto", auto_command))
        self._app.add_handler(CallbackQueryHandler(config_callback, pattern=r"^cfg:"))
        # Text handler for config value input (leverage, risk limits) — low priority
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, config_text_handler), group=1)

        # Trade views
        self._app.add_handler(CommandHandler("trades", trades_command))
        self._app.add_handler(CommandHandler("history", history_command))
        self._app.add_handler(CommandHandler("stats", stats_command))
        self._app.add_handler(CallbackQueryHandler(trade_detail_callback, pattern=r"^(trade:|trades_page:|history_page:|back:trades|noop)"))

        # Trade approval (Approve/Reject from push notifications, Close Position)
        self._app.add_handler(CallbackQueryHandler(signal_approval_callback, pattern=r"^signal:(approve|reject):"))
        self._app.add_handler(CallbackQueryHandler(close_trade_callback, pattern=r"^close_trade:"))
        self._app.add_handler(CallbackQueryHandler(confirm_close_callback, pattern=r"^(confirm_close|cancel_close):"))

        # Admin commands
        self._app.add_handler(CommandHandler("generate_code", generate_code_command))
        self._app.add_handler(CommandHandler("generate_codes", generate_codes_command))
        self._app.add_handler(CommandHandler("list_codes", list_codes_command))
        self._app.add_handler(CommandHandler("revoke_code", revoke_code_command))
