"""Potion Perps Bot — Entry point.

Loads config, connects to exchange, and runs the signal processing pipeline
with the configured input adapter. Handles graceful shutdown via SIGTERM/SIGINT.
"""

import asyncio
import logging
import signal
import sys

from src.config import Config, load_config
from src.exchange.hyperliquid import HyperliquidClient
from src.exchange.position_manager import PositionManager
from src.health import HealthServer
from src.input.cli_adapter import CLIAdapter
from src.input.simulation_adapter import SimulationAdapter
from src.pipeline import Pipeline
from src.state.database import TradeDatabase
from src.utils.logger import setup_logging


async def run(config: Config, user_id: str = "default") -> None:
    """Main loop: read signals from adapter, process through pipeline."""
    logger = logging.getLogger(__name__)

    shutdown_event = asyncio.Event()

    # --- Register signal handlers ---
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    # --- Connect to exchange ---
    client = HyperliquidClient(
        account_address=config.exchange.account_address,
        private_key=config.exchange.api_secret,
        network=config.exchange.network,
    )
    balance = client.get_balance()
    logger.info("Connected to %s — balance: $%s USDC", config.exchange.network, balance["usdc_balance"])

    # --- Initialize database ---
    db = TradeDatabase(user_id=user_id, db_path=config.database.path)

    # --- Sync positions with exchange ---
    pm = PositionManager(client, db)
    sync_result = pm.sync_positions()
    if sync_result["closed"] or sync_result["canceled"]:
        logger.warning(
            "Sync updated trades: %d closed, %d canceled",
            len(sync_result["closed"]), len(sync_result["canceled"]),
        )
    if sync_result["orphans"]:
        logger.warning("Orphan positions on exchange: %s", sync_result["orphans"])

    # --- Initialize pipeline ---
    pipeline = Pipeline(config=config, client=client, db=db)

    # --- Start health server ---
    health_server = HealthServer(port=config.health.port)
    if config.health.enabled:
        await health_server.start()
        logger.info("Health server listening on port %d", config.health.port)

    # --- Select input adapter ---
    adapter_name = config.input.adapter
    if adapter_name == "cli":
        adapter = CLIAdapter()
    elif adapter_name == "simulation":
        adapter = SimulationAdapter(
            signals_dir=config.input.simulation_dir,
            delay_sec=config.input.simulation_delay_sec,
        )
    else:
        logger.error("Unknown adapter: %s (use 'cli' or 'simulation')", adapter_name)
        return

    logger.info("Starting with adapter=%s, preset=%s, auto_execute=%s",
                adapter_name, config.strategy.active_preset, config.strategy.auto_execute)

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
            pipeline.process_message(raw_message)
            health_server.record_message()
    except Exception:
        logger.exception("Unexpected error in main loop")
    finally:
        logger.info("Shutting down...")
        adapter_task.cancel()
        try:
            await adapter_task
        except asyncio.CancelledError:
            pass
        await health_server.stop()
        db.close()
        logger.info("Shutdown complete")


def main() -> None:
    """Parse args and run."""
    user_id = "default"
    if len(sys.argv) > 1:
        user_id = sys.argv[1]

    config = load_config()
    setup_logging(config.logging)
    asyncio.run(run(config, user_id))


if __name__ == "__main__":
    main()
