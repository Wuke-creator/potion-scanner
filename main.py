"""Potion Perps Bot — Entry point.

Loads config, initializes the multi-user orchestrator, starts the admin API,
and runs the signal processing loop with the configured input adapter.
Handles graceful shutdown via SIGTERM/SIGINT.
"""

import asyncio
import logging
import signal

from src.api.admin import AdminAPI
from src.config import Config, load_config
from src.health import HealthServer
from src.input.cli_adapter import CLIAdapter
from src.input.simulation_adapter import SimulationAdapter
from src.orchestrator import Orchestrator
from src.state.user_db import UserDatabase
from src.utils.logger import setup_logging


async def run(config: Config) -> None:
    """Main loop: initialize orchestrator, start services, process signals."""
    logger = logging.getLogger(__name__)

    shutdown_event = asyncio.Event()

    # --- Register signal handlers ---
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    # --- Initialize user database & orchestrator ---
    user_db = UserDatabase(db_path=config.database.path)
    orchestrator = Orchestrator(global_config=config, user_db=user_db)

    # --- Start orchestrator (loads active users or falls back to single-user) ---
    orchestrator.start()

    # --- Start health server ---
    health_server = HealthServer(port=config.health.port)
    if config.health.enabled:
        await health_server.start()
        logger.info("Health server listening on port %d", config.health.port)

    # --- Start admin API ---
    admin_api = AdminAPI(
        user_db=user_db,
        on_user_activate=_make_activate_callback(orchestrator),
        on_user_deactivate=_make_deactivate_callback(orchestrator),
        on_kill=_make_kill_callback(orchestrator),
        on_resume=_make_resume_callback(orchestrator),
    )
    await admin_api.start()

    # --- Start Telegram bot (if token configured) ---
    telegram_bot = None
    if config.telegram.bot_token:
        from src.telegram.bot import TelegramBot
        from src.telegram.notifications import TelegramNotifier

        telegram_bot = TelegramBot(
            token=config.telegram.bot_token,
            user_db=user_db,
            config=config,
            orchestrator=orchestrator,
        )
        await telegram_bot.start()

        # Create notifier factory now that the bot is running
        from src.telegram.handlers.menu import (
            _build_calls_text,
            get_calls_view_msg,
            set_calls_view_msg,
        )

        def _notifier_factory(uid: str) -> TelegramNotifier:
            chat_id = user_db.get_telegram_chat_id(uid)

            def _checker() -> bool:
                calls_set = telegram_bot._app.bot_data.get("calls_view_users", set())
                return chat_id in calls_set

            async def _refresher() -> None:
                """Delete old calls-view message and send updated one."""
                bot_data = telegram_bot._app.bot_data
                bot = telegram_bot.bot
                # Build a minimal context-like object for _build_calls_text
                class _Ctx:
                    pass
                fake_ctx = _Ctx()
                fake_ctx.bot_data = bot_data

                text, keyboard = _build_calls_text(fake_ctx, uid)

                # Delete old calls-view message
                old_msg_id = get_calls_view_msg(bot_data, chat_id)
                if old_msg_id:
                    try:
                        await bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
                    except Exception:
                        pass

                # Send fresh calls view and track the new message id
                msg = await bot.send_message(
                    chat_id=chat_id, text=text,
                    parse_mode="Markdown", reply_markup=keyboard,
                )
                set_calls_view_msg(bot_data, chat_id, msg.message_id)

            return TelegramNotifier(
                bot=telegram_bot.bot,
                user_db=user_db,
                user_id=uid,
                calls_view_checker=_checker,
                calls_view_refresher=_refresher,
            )

        orchestrator._notifier_factory = _notifier_factory

        # Re-attach notifiers to any pipelines already activated
        for uid, ctx in orchestrator.pipelines.items():
            ctx.pipeline._notifier = _notifier_factory(uid)

        # Start expiry checker background task
        from src.telegram.expiry_checker import ExpiryChecker
        expiry_checker = ExpiryChecker(
            bot=telegram_bot.bot,
            user_db=user_db,
            orchestrator=orchestrator,
        )
        await expiry_checker.start()

        logger.info("Telegram bot started with trade notifications")
    else:
        expiry_checker = None
        logger.info("Telegram bot disabled (no TELEGRAM_BOT_TOKEN in .env)")

    # --- Select input adapter ---
    adapter_name = config.input.adapter
    if adapter_name == "cli":
        adapter = CLIAdapter()
    elif adapter_name == "simulation":
        adapter = SimulationAdapter(
            signals_dir=config.input.simulation_dir,
            delay_sec=config.input.simulation_delay_sec,
        )
    elif adapter_name == "discord":
        from src.input.discord_adapter import DiscordAdapter
        adapter = DiscordAdapter(
            bot_token=config.discord.bot_token,
            channel_id=config.discord.channel_id,
            source_bot_name=config.discord.source_bot_name,
        )
    else:
        logger.error("Unknown adapter: %s (use 'cli', 'simulation', or 'discord')", adapter_name)
        return

    logger.info(
        "Starting with adapter=%s, %d active pipeline(s)",
        adapter_name, len(orchestrator.pipelines),
    )

    # --- Run ---
    adapter_task = asyncio.create_task(adapter.start())

    try:
        while not shutdown_event.is_set():
            try:
                raw_message = await asyncio.wait_for(adapter.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if not raw_message.strip():
                continue
            logger.info("--- Incoming message (%d chars) ---", len(raw_message))
            orchestrator.dispatch(raw_message, health_server)
    except Exception:
        logger.exception("Unexpected error in main loop")
    finally:
        logger.info("Shutting down...")
        adapter_task.cancel()
        try:
            await adapter_task
        except asyncio.CancelledError:
            pass
        if expiry_checker:
            await expiry_checker.stop()
        if telegram_bot:
            await telegram_bot.stop()
        await admin_api.stop()
        await health_server.stop()
        orchestrator.stop()
        user_db.close()
        logger.info("Shutdown complete")


def _make_activate_callback(orchestrator: Orchestrator):
    """Create an async callback for user activation."""
    async def callback(user_id: str) -> None:
        orchestrator.activate_user(user_id)
    return callback


def _make_deactivate_callback(orchestrator: Orchestrator):
    """Create an async callback for user deactivation."""
    async def callback(user_id: str) -> None:
        orchestrator.deactivate_user(user_id)
    return callback


def _make_kill_callback(orchestrator: Orchestrator):
    """Create an async callback for the kill switch."""
    async def callback() -> dict:
        return orchestrator.kill_all()
    return callback


def _make_resume_callback(orchestrator: Orchestrator):
    """Create an async callback to resume after kill switch."""
    async def callback() -> None:
        orchestrator.resume()
    return callback


def main() -> None:
    """Parse args and run."""
    config = load_config()
    setup_logging(config.logging)
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
