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
    )
    await admin_api.start()

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


def main() -> None:
    """Parse args and run."""
    config = load_config()
    setup_logging(config.logging)
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
