"""Background task — monitors positions for PnL threshold alerts.

Checks every 60 seconds. Sends alerts when positions hit:
- +5% unrealized profit
- -3% unrealized loss

Uses in-memory deduplication so each (user, coin, threshold_type) combo
is alerted only once. Clears when position disappears or PnL drops back
below threshold.
"""

import asyncio
import logging

from src.orchestrator import Orchestrator
from src.state.user_db import UserDatabase

logger = logging.getLogger(__name__)

PROFIT_THRESHOLD_PCT = 5.0
LOSS_THRESHOLD_PCT = -3.0


class PnLMonitor:
    """Periodic background task that checks positions for PnL thresholds."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        user_db: UserDatabase,
        interval_sec: float = 60.0,
    ) -> None:
        self._orchestrator = orchestrator
        self._user_db = user_db
        self._interval_sec = interval_sec
        self._alerted: set[tuple[str, str, str]] = set()  # (user_id, coin, "profit"/"loss")
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Launch the background loop."""
        self._task = asyncio.create_task(self._loop())
        logger.info("PnLMonitor started (interval=%ds)", self._interval_sec)

    async def stop(self) -> None:
        """Cancel the background loop."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("PnLMonitor stopped")

    async def _loop(self) -> None:
        """Run check_positions on a fixed interval."""
        while True:
            try:
                await self.check_positions()
            except Exception:
                logger.exception("PnLMonitor tick failed")
            await asyncio.sleep(self._interval_sec)

    async def check_positions(self) -> dict:
        """Single check pass. Returns summary for testability.

        Returns:
            {"alerts": [(user_id, coin, threshold_type, pnl_pct), ...],
             "cleared": [(user_id, coin, threshold_type), ...]}
        """
        result: dict[str, list] = {"alerts": [], "cleared": []}
        active_keys: set[tuple[str, str]] = set()  # (user_id, coin) seen this tick

        for user_id, ctx in self._orchestrator.pipelines.items():
            if ctx.paused:
                continue

            notifier = getattr(ctx.pipeline, "_notifier", None)
            if notifier is None:
                continue

            try:
                positions = ctx.client.get_open_positions()
            except Exception:
                logger.exception("PnLMonitor: failed to fetch positions for user %s", user_id)
                continue

            for pos in positions:
                coin = pos.get("coin", "")
                entry_price = float(pos.get("entryPx", 0))
                size = float(pos.get("szi", pos.get("size", 0)))
                unrealized_pnl = float(pos.get("unrealizedPnl", 0))

                active_keys.add((user_id, coin))

                if entry_price == 0 or size == 0:
                    continue

                pnl_pct = unrealized_pnl / (entry_price * abs(size)) * 100
                side = "LONG" if size > 0 else "SHORT"

                # Check profit threshold
                if pnl_pct >= PROFIT_THRESHOLD_PCT:
                    key = (user_id, coin, "profit")
                    if key not in self._alerted:
                        self._alerted.add(key)
                        try:
                            await notifier.notify_pnl_alert(coin, side, pnl_pct, "profit")
                        except Exception:
                            logger.exception("PnLMonitor: failed to send profit alert")
                        result["alerts"].append((user_id, coin, "profit", pnl_pct))
                else:
                    # PnL dropped back below profit threshold — clear so it can re-alert
                    key = (user_id, coin, "profit")
                    if key in self._alerted:
                        self._alerted.discard(key)
                        result["cleared"].append((user_id, coin, "profit"))

                # Check loss threshold
                if pnl_pct <= LOSS_THRESHOLD_PCT:
                    key = (user_id, coin, "loss")
                    if key not in self._alerted:
                        self._alerted.add(key)
                        try:
                            await notifier.notify_pnl_alert(coin, side, pnl_pct, "loss")
                        except Exception:
                            logger.exception("PnLMonitor: failed to send loss alert")
                        result["alerts"].append((user_id, coin, "loss", pnl_pct))
                else:
                    key = (user_id, coin, "loss")
                    if key in self._alerted:
                        self._alerted.discard(key)
                        result["cleared"].append((user_id, coin, "loss"))

        # Clear alerts for positions that no longer exist
        stale_keys = [
            k for k in self._alerted
            if (k[0], k[1]) not in active_keys
        ]
        for key in stale_keys:
            self._alerted.discard(key)
            result["cleared"].append(key)

        if result["alerts"] or result["cleared"]:
            logger.info(
                "PnLMonitor: %d alerts, %d cleared",
                len(result["alerts"]), len(result["cleared"]),
            )

        return result
