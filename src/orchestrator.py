"""Multi-user orchestrator — one signal source fans out to N user pipelines.

Each user gets an isolated Pipeline with their own Config, HyperliquidClient,
and TradeDatabase. Errors in one user's pipeline do not affect others.
"""

import logging
import os
from dataclasses import dataclass

from src.config.settings import Config
from src.exchange.hyperliquid import HyperliquidClient
from src.exchange.position_manager import PositionManager
from src.health import HealthServer
from src.pipeline import Pipeline
from src.state.database import TradeDatabase
from src.state.user_db import UserDatabase

logger = logging.getLogger(__name__)


@dataclass
class UserPipelineContext:
    """All per-user runtime objects."""

    user_id: str
    config: Config
    client: HyperliquidClient
    db: TradeDatabase
    pipeline: Pipeline


class Orchestrator:
    """Manages a dict of user pipeline contexts and dispatches signals to all.

    Supports hot-adding and hot-removing users via activate/deactivate.
    Falls back to single-user mode when no DB users exist and .env has credentials.
    """

    def __init__(self, global_config: Config, user_db: UserDatabase):
        self._global_config = global_config
        self._user_db = user_db
        self._pipelines: dict[str, UserPipelineContext] = {}

    @property
    def pipelines(self) -> dict[str, UserPipelineContext]:
        return self._pipelines

    def start(self) -> None:
        """Load all active users from DB and create pipeline contexts.

        If no DB users exist and .env has HL_ACCOUNT_ADDRESS, falls back
        to single-user "default" pipeline using global config.
        """
        active_users = self._user_db.get_active_users()

        if active_users:
            for user in active_users:
                try:
                    self.activate_user(user.user_id)
                except Exception as e:
                    logger.error("Failed to activate user %s on startup: %s", user.user_id, e)
            logger.info("Orchestrator started with %d user(s)", len(self._pipelines))
        elif os.getenv("HL_ACCOUNT_ADDRESS"):
            # Backward compatibility: single-user mode from .env
            self._activate_default_user()
            logger.info("Orchestrator started in single-user fallback mode")
        else:
            logger.warning("Orchestrator started with no users (add users via admin API)")

    def activate_user(self, user_id: str) -> None:
        """Build and register a pipeline for the given user.

        Decrypts credentials, builds per-user Config, creates exchange client,
        database, and pipeline. Syncs positions on activation.
        """
        if user_id in self._pipelines:
            logger.warning("User %s already active, skipping", user_id)
            return

        user_config = self._user_db.get_user_config_as_config(user_id, self._global_config)

        client = HyperliquidClient(
            account_address=user_config.exchange.account_address,
            private_key=user_config.exchange.api_secret,
            network=user_config.exchange.network,
        )

        db = TradeDatabase(user_id=user_id, db_path=self._global_config.database.path)

        # Sync positions
        pm = PositionManager(client, db)
        sync_result = pm.sync_positions()
        if sync_result["closed"] or sync_result["canceled"]:
            logger.warning(
                "User %s sync: %d closed, %d canceled",
                user_id, len(sync_result["closed"]), len(sync_result["canceled"]),
            )

        pipeline = Pipeline(config=user_config, client=client, db=db)

        self._pipelines[user_id] = UserPipelineContext(
            user_id=user_id,
            config=user_config,
            client=client,
            db=db,
            pipeline=pipeline,
        )
        logger.info("Activated pipeline for user %s", user_id)

    def deactivate_user(self, user_id: str) -> None:
        """Remove and clean up a user's pipeline."""
        ctx = self._pipelines.pop(user_id, None)
        if ctx:
            ctx.db.close()
            logger.info("Deactivated pipeline for user %s", user_id)
        else:
            logger.warning("User %s not found in active pipelines", user_id)

    def dispatch(self, raw_message: str, health_server: HealthServer | None = None) -> None:
        """Fan out a signal to all active pipelines.

        Each pipeline is called in a try/except so one user's error
        doesn't crash others.
        """
        if not self._pipelines:
            logger.warning("No active pipelines — message ignored")
            return

        for user_id, ctx in self._pipelines.items():
            try:
                ctx.pipeline.process_message(raw_message)
            except Exception:
                logger.exception("Error processing message for user %s", user_id)

        if health_server:
            health_server.record_message()

    def stop(self) -> None:
        """Shut down all user pipelines."""
        for user_id in list(self._pipelines.keys()):
            self.deactivate_user(user_id)
        logger.info("Orchestrator stopped — all pipelines closed")

    def _activate_default_user(self) -> None:
        """Backward-compatible single-user mode from .env + global config."""
        user_id = "default"
        client = HyperliquidClient(
            account_address=self._global_config.exchange.account_address,
            private_key=self._global_config.exchange.api_secret,
            network=self._global_config.exchange.network,
        )

        db = TradeDatabase(user_id=user_id, db_path=self._global_config.database.path)

        pm = PositionManager(client, db)
        sync_result = pm.sync_positions()
        if sync_result["closed"] or sync_result["canceled"]:
            logger.warning(
                "Default user sync: %d closed, %d canceled",
                len(sync_result["closed"]), len(sync_result["canceled"]),
            )

        pipeline = Pipeline(config=self._global_config, client=client, db=db)

        self._pipelines[user_id] = UserPipelineContext(
            user_id=user_id,
            config=self._global_config,
            client=client,
            db=db,
            pipeline=pipeline,
        )
        logger.info("Activated default single-user pipeline")
