"""Potion Perps Bot — Entry point.

Loads config, connects to exchange, and runs the signal processing pipeline
with the configured input adapter.
"""

import asyncio
import logging
import sys

from src.config import Config, load_config
from src.exchange.hyperliquid import HyperliquidClient
from src.input.cli_adapter import CLIAdapter
from src.input.simulation_adapter import SimulationAdapter
from src.pipeline import Pipeline
from src.state.database import TradeDatabase


def _setup_logging(config: Config) -> None:
    """Configure logging from config settings."""
    from pathlib import Path

    log_path = Path(config.logging.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, config.logging.level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_path),
        ],
    )


async def run(config: Config, user_id: str = "default") -> None:
    """Main loop: read signals from adapter, process through pipeline."""
    logger = logging.getLogger(__name__)

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

    # --- Initialize pipeline ---
    pipeline = Pipeline(config=config, client=client, db=db)

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
        while True:
            raw_message = await adapter.queue.get()
            if not raw_message.strip():
                continue
            logger.info("--- Incoming message (%d chars) ---", len(raw_message))
            pipeline.process_message(raw_message)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        adapter_task.cancel()
        db.close()


def main() -> None:
    """Parse args and run."""
    user_id = "default"
    if len(sys.argv) > 1:
        user_id = sys.argv[1]

    config = load_config()
    _setup_logging(config)
    asyncio.run(run(config, user_id))


if __name__ == "__main__":
    main()
