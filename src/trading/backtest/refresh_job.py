"""Scheduled re-run of the copier backtest, so the scout's latency gate
never silently lapses.

The scout only trusts a backtest verdict for 30 days
(``wallet_scout.py``: ``bt_at > time.time() - 30 * 86_400``). Until now the
only way to produce a verdict at all was the manual Telegram ``/backtest``
command, so ``bt_latency_ratio`` / ``bt_copier_net`` sat null on every
wallet and scoring v2 ran without its latency term. This job closes that
gap: once a night it re-runs the tracked set through the SAME
``BacktestRunner`` the command uses, which writes the verdict back via
``record_backtest_fitness`` as a side effect. No new writeback logic here
on purpose, so the scheduled and manual paths can never drift.

DARK BY DEFAULT. ``BACKTEST_REFRESH_ENABLED`` gates it entirely. Like the
snapshot job it is read-only with respect to trading: it replays history
and places no orders, sends no DMs, and raises no proposals.

Runs at ``BACKTEST_REFRESH_HOUR_UTC`` (default 04:00, an hour after the
03:00 snapshot archiver so it sees the freshest candles/funding, which are
two hours after the 02:00 scout).

Only wallets whose verdict is missing or older than
``BACKTEST_REFRESH_MAX_AGE_DAYS`` (default 21) are re-run. That keeps the
nightly cost near zero while staying inside the scout's 30-day window, so
a verdict is refreshed a week before it would expire.

Known limitation: deference is one-directional. If a manual ``/backtest``
is already running this job skips the night (``busy`` predicate). A manual
run started *during* a scheduled run is NOT blocked, because the command's
job manager does not know about this job. Two concurrent runs would waste
Hyperliquid budget and double-write the same verdict, not corrupt it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable

from src.config.settings import BacktestConfig
from src.trading.backtest.runner import (
    PRIMARY_DELAY_SEC,
    BacktestRunner,
    BacktestSpec,
)
from src.trading.wallet_metrics_db import WalletMetricsDB
from src.trading.wallet_scout import short_addr

logger = logging.getLogger(__name__)

_LABEL = "nightly-refresh"


class BacktestRefreshJob:
    def __init__(
        self,
        *,
        runner: BacktestRunner,
        metrics_db: WalletMetricsDB,
        backtest_cfg: BacktestConfig,
        busy: Callable[[], bool] | None = None,
    ):
        self._runner = runner
        self._metrics_db = metrics_db
        self._cfg = backtest_cfg
        self._busy = busy
        self._running = False

    @property
    def running(self) -> bool:
        """True while a scheduled pass is in flight."""
        return self._running

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self._seconds_until_next_run())
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("backtest refresh run crashed")

    def _seconds_until_next_run(self) -> float:
        now = datetime.now(timezone.utc)
        target = now.replace(
            hour=self._cfg.refresh_hour_utc, minute=0, second=0, microsecond=0,
        )
        secs = (target - now).total_seconds()
        if secs <= 60:
            secs += 86_400
        return secs

    async def due_addresses(self, *, now: float | None = None) -> list[str]:
        """Tracked (and optionally candidate) wallets whose verdict is
        missing or about to expire, worst-scored last so the most relevant
        wallets are simulated first if the run times out."""
        now = time.time() if now is None else now
        cutoff = now - self._cfg.refresh_max_age_days * 86_400
        wallets = await self._metrics_db.list_wallets(
            status=None if self._cfg.refresh_include_candidates else "tracked",
        )
        return [
            w.address for w in wallets
            if w.bt_copier_net is None or not w.bt_at or w.bt_at < cutoff
        ]

    async def run_once(self) -> dict:
        """One refresh pass. Returns a summary; never raises for a single
        wallet's failure (the runner isolates those itself)."""
        summary: dict = {
            "skipped": "", "requested": 0, "completed": 0, "errors": 0,
            "verdicts": {},
        }

        if self._busy is not None and self._busy():
            summary["skipped"] = "a manual backtest is running"
            logger.info("backtest refresh skipped: %s", summary["skipped"])
            return summary

        addresses = await self.due_addresses()
        summary["requested"] = len(addresses)
        if not addresses:
            summary["skipped"] = "every verdict is still fresh"
            logger.info("backtest refresh: nothing due")
            return summary

        async def progress(text: str) -> None:
            logger.debug("backtest refresh: %s", text)

        timeout = self._cfg.job_timeout_min * 60
        self._running = True
        try:
            result = await asyncio.wait_for(
                self._runner.run(
                    BacktestSpec(
                        addresses=addresses,
                        days=self._cfg.refresh_days,
                        label=_LABEL,
                    ),
                    progress,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            summary["skipped"] = f"timed out after {self._cfg.job_timeout_min} min"
            logger.warning(
                "backtest refresh timed out after %d min on %d wallet(s)",
                self._cfg.job_timeout_min, len(addresses),
            )
            return summary
        finally:
            self._running = False

        for wr in result.wallets:
            if wr.error:
                summary["errors"] += 1
                logger.warning(
                    "backtest refresh: %s failed: %s",
                    short_addr(wr.address), wr.error,
                )
                continue
            summary["completed"] += 1
            summary["verdicts"][wr.address] = wr.net_by_delay.get(
                PRIMARY_DELAY_SEC,
            )

        logger.info(
            "backtest refresh: requested=%d completed=%d errors=%d verdicts=%s",
            summary["requested"], summary["completed"], summary["errors"],
            {
                short_addr(a): (round(v, 2) if v is not None else None)
                for a, v in summary["verdicts"].items()
            },
        )
        return summary
