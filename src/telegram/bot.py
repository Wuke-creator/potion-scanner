"""Telegram bot — core class.

Wraps python-telegram-bot's Application to integrate with the
existing async main loop. Starts and stops cleanly alongside
the rest of the system.
"""

import logging

from telegram import BotCommand, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, TypeHandler, filters

from src.config.settings import Config
from src.orchestrator import Orchestrator
from src.state.user_db import UserDatabase
from src.telegram.handlers.account import (
    account_nav_callback,
    activate_command,
    balance_command,
    deactivate_command,
    positions_command,
    status_command,
)
from src.telegram.handlers.approval import (
    close_trade_callback,
    confirm_close_callback,
    signal_approval_callback,
)
from src.telegram.handlers.admin import (
    add_admin_command,
    admin_callback,
    admin_help_command,
    broadcast_command,
    extend_command,
    generate_code_command,
    generate_codes_command,
    inject_callback,
    inject_command,
    kill_command,
    list_admins_command,
    list_codes_command,
    remove_admin_command,
    resume_command,
    revoke_code_command,
    revoke_command,
    users_command,
)
from src.telegram.handlers.config import (
    auto_command,
    config_callback,
    config_command,
    config_text_handler,
    preset_command,
)
from src.telegram.handlers.help import cancel_command, help_command, start_command, start_register_callback, unknown_command
from src.telegram.handlers.menu import account_renew_callback, menu_callback, menu_command, renew_code_text_handler, renew_command
from src.telegram.middleware import dm_only_filter, global_error_handler, rate_limit_filter
from src.telegram.handlers.registration import build_registration_handler
from src.telegram.handlers.trades import (
    history_command,
    stats_command,
    trade_detail_callback,
    trades_command,
    trading_callback,
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
        # Merge env-based admin IDs with DB-stored admin IDs
        admin_ids = list(self._config.telegram.admin_ids)
        for db_admin in self._user_db.list_telegram_admins():
            if db_admin not in admin_ids:
                admin_ids.append(db_admin)
        self._app.bot_data["admin_ids"] = admin_ids
        self._app.bot_data["orchestrator"] = self._orchestrator

        self._register_handlers()

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

        # Register command menu (autocomplete suggestions when typing /)
        await self._app.bot.set_my_commands([
            BotCommand("menu", "Open main menu"),
            BotCommand("start", "Welcome / main menu"),
            BotCommand("help", "Show available commands"),
            BotCommand("register", "Register with an invite code"),
            BotCommand("balance", "Account balance"),
            BotCommand("positions", "Open positions"),
            BotCommand("trades", "Active trades"),
            BotCommand("history", "Trade history"),
            BotCommand("stats", "Trading statistics"),
            BotCommand("status", "Risk dashboard"),
            BotCommand("config", "View & change settings"),
            BotCommand("renew", "Renew access with a new code"),
            BotCommand("cancel", "Cancel current action"),
            BotCommand("admin", "Admin commands"),
        ])

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
        # Pre-processing: DM-only and rate limiting (group -1 runs before all handlers)
        self._app.add_handler(TypeHandler(Update, dm_only_filter), group=-2)
        self._app.add_handler(TypeHandler(Update, rate_limit_filter), group=-1)

        # Global error handler
        self._app.add_error_handler(global_error_handler)

        self._app.add_handler(CommandHandler("start", start_command))
        self._app.add_handler(CommandHandler("help", help_command))
        self._app.add_handler(CommandHandler("menu", menu_command))
        self._app.add_handler(CommandHandler("cancel", cancel_command))
        self._app.add_handler(CommandHandler("renew", renew_command))

        # Registration conversation (must be before simple command handlers)
        self._app.add_handler(build_registration_handler())

        # Menu navigation callbacks
        self._app.add_handler(CallbackQueryHandler(account_renew_callback, pattern=r"^account:renew$"))
        self._app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
        self._app.add_handler(CallbackQueryHandler(start_register_callback, pattern=r"^start:register$"))

        # Account monitoring (command shortcuts)
        self._app.add_handler(CommandHandler("balance", balance_command))
        self._app.add_handler(CommandHandler("positions", positions_command))
        self._app.add_handler(CommandHandler("status", status_command))
        self._app.add_handler(CommandHandler("activate", activate_command))
        self._app.add_handler(CommandHandler("deactivate", deactivate_command))
        self._app.add_handler(CallbackQueryHandler(account_nav_callback, pattern=r"^nav:"))

        # Configuration
        self._app.add_handler(CommandHandler("config", config_command))
        self._app.add_handler(CommandHandler("preset", preset_command))
        self._app.add_handler(CommandHandler("auto", auto_command))
        self._app.add_handler(CallbackQueryHandler(config_callback, pattern=r"^cfg:"))
        # Text handler for renewal code input
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, renew_code_text_handler), group=1)
        # Text handler for config value input (leverage, risk limits) — low priority
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, config_text_handler), group=2)

        # Trade views
        self._app.add_handler(CommandHandler("trades", trades_command))
        self._app.add_handler(CommandHandler("history", history_command))
        self._app.add_handler(CommandHandler("stats", stats_command))
        self._app.add_handler(CallbackQueryHandler(trade_detail_callback, pattern=r"^(trade:|trades_page:|history_page:|back:trades|noop)"))
        self._app.add_handler(CallbackQueryHandler(trading_callback, pattern=r"^trading:"))

        # Trade approval (Approve/Reject from push notifications, Close Position)
        self._app.add_handler(CallbackQueryHandler(signal_approval_callback, pattern=r"^signal:(approve|reject):"))
        self._app.add_handler(CallbackQueryHandler(close_trade_callback, pattern=r"^close_trade:"))
        self._app.add_handler(CallbackQueryHandler(confirm_close_callback, pattern=r"^(confirm_close|cancel_close):"))

        # Admin commands
        self._app.add_handler(CommandHandler("admin", admin_help_command))
        self._app.add_handler(CommandHandler("generate_code", generate_code_command))
        self._app.add_handler(CommandHandler("generate_codes", generate_codes_command))
        self._app.add_handler(CommandHandler("list_codes", list_codes_command))
        self._app.add_handler(CommandHandler("revoke_code", revoke_code_command))
        self._app.add_handler(CommandHandler("users", users_command))
        self._app.add_handler(CommandHandler("extend", extend_command))
        self._app.add_handler(CommandHandler("revoke", revoke_command))
        self._app.add_handler(CommandHandler("kill", kill_command))
        self._app.add_handler(CommandHandler("resume", resume_command))
        self._app.add_handler(CommandHandler("broadcast", broadcast_command))
        self._app.add_handler(CommandHandler("add_admin", add_admin_command))
        self._app.add_handler(CommandHandler("remove_admin", remove_admin_command))
        self._app.add_handler(CommandHandler("list_admins", list_admins_command))
        self._app.add_handler(CommandHandler("inject", inject_command))
        self._app.add_handler(CallbackQueryHandler(inject_callback, pattern=r"^inject:"))
        self._app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin:"))

        # Unknown command handler — must be last
        self._app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
