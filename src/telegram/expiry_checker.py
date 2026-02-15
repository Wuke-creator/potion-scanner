"""Background task — checks for expired and soon-to-expire users.

Runs periodically (default: every hour). On each tick:
1. Finds users whose access has expired → deactivates pipeline, sets inactive, notifies.
2. Finds users expiring within 3 days → sends warning (once per window).
3. Finds users expiring within 1 day → sends urgent warning (once per window).

Warning deduplication: uses an in-memory set of (user_id, window) pairs so each
user gets at most one "3 day" and one "1 day" warning per expiry cycle.
"""

import asyncio
import logging
from datetime import datetime

from telegram import Bot

from src.orchestrator import Orchestrator
from src.state.user_db import UserDatabase

logger = logging.getLogger(__name__)


class ExpiryChecker:
    """Periodic background task that enforces access expiry."""

    def __init__(
        self,
        bot: Bot,
        user_db: UserDatabase,
        orchestrator: Orchestrator,
        interval_sec: float = 3600.0,
    ) -> None:
        self._bot = bot
        self._user_db = user_db
        self._orchestrator = orchestrator
        self._interval_sec = interval_sec
        self._warned: set[tuple[str, str]] = set()  # (user_id, "3d"/"1d")
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Launch the background loop."""
        self._task = asyncio.create_task(self._loop())
        logger.info("ExpiryChecker started (interval=%ds)", self._interval_sec)

    async def stop(self) -> None:
        """Cancel the background loop."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ExpiryChecker stopped")

    async def _loop(self) -> None:
        """Run check_expiry on a fixed interval."""
        while True:
            try:
                await self.check_expiry()
            except Exception:
                logger.exception("ExpiryChecker tick failed")
            await asyncio.sleep(self._interval_sec)

    async def check_expiry(self) -> dict:
        """Single check pass. Returns summary dict for testability.

        Returns:
            {"expired": [user_ids], "warned_3d": [user_ids], "warned_1d": [user_ids]}
        """
        result: dict[str, list[str]] = {"expired": [], "warned_3d": [], "warned_1d": []}

        # --- Handle expired users ---
        expired_users = self._user_db.get_expired_users()
        for user_id in expired_users:
            logger.warning("User %s access expired — deactivating", user_id)
            self._user_db.set_user_status(user_id, "inactive")
            self._orchestrator.deactivate_user(user_id)

            # Clear warnings so they can be re-sent if re-activated and re-expired
            self._warned.discard((user_id, "3d"))
            self._warned.discard((user_id, "1d"))

            # Notify user
            chat_id = self._user_db.get_telegram_chat_id(user_id)
            if chat_id:
                try:
                    await self._bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "*Access Expired*\n\n"
                            "Your access has expired and trading has been deactivated.\n"
                            "Contact admin to renew your access."
                        ),
                        parse_mode="Markdown",
                    )
                except Exception:
                    logger.exception("Failed to send expiry notification to user %s", user_id)

            result["expired"].append(user_id)

        # --- 3-day warnings ---
        expiring_3d = self._user_db.get_users_expiring_within(hours=72)
        for user_id, expires_at in expiring_3d:
            if (user_id, "3d") in self._warned:
                continue
            self._warned.add((user_id, "3d"))

            expires_date = expires_at[:10]
            chat_id = self._user_db.get_telegram_chat_id(user_id)
            if chat_id:
                try:
                    await self._bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "*Access Expiry Warning*\n\n"
                            f"Your access expires on {expires_date}.\n"
                            "Contact admin to extend your access."
                        ),
                        parse_mode="Markdown",
                    )
                except Exception:
                    logger.exception("Failed to send 3d warning to user %s", user_id)

            result["warned_3d"].append(user_id)

        # --- 1-day warnings ---
        expiring_1d = self._user_db.get_users_expiring_within(hours=24)
        for user_id, expires_at in expiring_1d:
            if (user_id, "1d") in self._warned:
                continue
            self._warned.add((user_id, "1d"))

            expires_dt = datetime.fromisoformat(expires_at)
            hours_left = max(0, (expires_dt - datetime.now(expires_dt.tzinfo)).total_seconds() / 3600)
            chat_id = self._user_db.get_telegram_chat_id(user_id)
            if chat_id:
                try:
                    await self._bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "*Urgent: Access Expiring Soon*\n\n"
                            f"Your access expires in {int(hours_left)} hours.\n"
                            "Trading will be deactivated when it expires.\n"
                            "Contact admin to extend your access."
                        ),
                        parse_mode="Markdown",
                    )
                except Exception:
                    logger.exception("Failed to send 1d warning to user %s", user_id)

            result["warned_1d"].append(user_id)

        if result["expired"] or result["warned_3d"] or result["warned_1d"]:
            logger.info(
                "ExpiryChecker: expired=%d, warned_3d=%d, warned_1d=%d",
                len(result["expired"]), len(result["warned_3d"]), len(result["warned_1d"]),
            )

        return result
