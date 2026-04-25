"""Wallet Tracker alert debouncer.

Onsight-style wallet trackers often emit several BUY/SELL events in quick
succession when a trader is DCAing into or scaling out of a position (e.g.
4 BUYs of the same token from the same trader over 30 seconds). Forwarding
each one verbatim spams subscribers.

This module consolidates rapid same-trader-same-token-same-action events
into a single Telegram alert. Grouping key:

    (trader.lower(), token.lower(), action.upper())

Emission policy:
  - First alert for a key starts a new buffer and a background flush task.
  - Subsequent alerts matching the same key accumulate into the buffer.
  - The flush task wakes every ``idle_timeout_sec`` and emits the batch
    if EITHER (a) no new alert has arrived in ``idle_timeout_sec`` OR
    (b) the buffer is older than ``max_hold_sec`` (prevents an
    actively-accumulating trader from holding the batch forever).

Persistence: in-memory only. If the bot restarts mid-window, in-flight
batches are lost. Acceptable for informational wallet tracker alerts —
they're not time-critical trading signals.

No thread safety needed; everything runs on the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from src.parser.wallet_tracker_parser import WalletTrackerAlert

logger = logging.getLogger(__name__)


EmitCallback = Callable[
    [WalletTrackerAlert, int], Awaitable[None]
]
"""Signature: ``async def emit(consolidated_alert, buy_count) -> None``."""


def _parse_number(s: str) -> float:
    """Best-effort parse of a comma/period-separated number string into a
    float. Returns 0.0 on empty or unparseable input so accumulation logic
    never raises."""
    if not s:
        return 0.0
    try:
        return float(s.replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0.0


def _format_sol(n: float) -> str:
    """Format a SOL amount with 2-6 decimal places, no trailing zeros
    beyond what's useful."""
    if n == 0:
        return ""
    if n >= 100:
        return f"{n:,.2f}"
    if n >= 1:
        return f"{n:,.3f}".rstrip("0").rstrip(".")
    return f"{n:.6f}".rstrip("0").rstrip(".")


def _format_usd(n: float) -> str:
    """Format a USD amount with 2 decimals + thousands separators."""
    if n == 0:
        return ""
    return f"{n:,.2f}"


def _format_amount(n: float) -> str:
    """Format a token amount with thousands separators."""
    if n == 0:
        return ""
    # For tiny memecoin amounts we want precision; for huge amounts a
    # compact integer display.
    if n >= 1_000_000:
        return f"{n:,.0f}"
    if n >= 1:
        return f"{n:,.2f}"
    return f"{n:.6f}".rstrip("0").rstrip(".")


@dataclass
class _Batch:
    """One accumulating consolidation batch for a single (trader, token,
    action) key."""

    first_alert: WalletTrackerAlert
    latest_alert: WalletTrackerAlert
    alerts: list[WalletTrackerAlert] = field(default_factory=list)
    first_seen: float = 0.0
    last_seen: float = 0.0

    def accumulate(self, alert: WalletTrackerAlert, now: float) -> None:
        self.alerts.append(alert)
        self.latest_alert = alert
        self.last_seen = now

    def to_consolidated(self) -> tuple[WalletTrackerAlert, int]:
        """Merge the batch into a single alert.

        Sums spent_sol / spent_usd / received_amount across the batch.
        Uses the LATEST alert for MC / Age / Holds / PnL / CA because
        those reflect the trader's current state at the moment we send.
        Leaves per-unit price blank — the formatter can compute an
        effective average from the totals when desired.

        Returns a tuple of (consolidated_alert, buy_count).
        """
        total_sol = sum(_parse_number(a.spent_sol) for a in self.alerts)
        total_usd = sum(_parse_number(a.spent_usd) for a in self.alerts)
        total_received = sum(
            _parse_number(a.received_amount) for a in self.alerts
        )

        # Effective price = total USD / total amount
        avg_price = ""
        if total_usd > 0 and total_received > 0:
            eff = total_usd / total_received
            # Use scientific notation for very small prices to avoid a
            # string of leading zeros.
            avg_price = f"{eff:.2e}" if eff < 0.0001 else f"{eff:.6f}".rstrip("0").rstrip(".")

        latest = self.latest_alert
        consolidated = WalletTrackerAlert(
            action=self.first_alert.action,
            token=self.first_alert.token,
            platform=latest.platform or self.first_alert.platform,
            trader=self.first_alert.trader,
            spent_sol=_format_sol(total_sol),
            spent_usd=_format_usd(total_usd),
            received_amount=_format_amount(total_received),
            price=avg_price,
            holds_amount=latest.holds_amount,
            holds_pct=latest.holds_pct,
            pnl=latest.pnl,
            pnl_positive=latest.pnl_positive,
            market_cap=latest.market_cap,
            age=latest.age,
            ca=latest.ca,
            raw_content="",
            parsed_ok=True,
        )
        return consolidated, len(self.alerts)


class WalletTrackerDebouncer:
    """Buffers and consolidates rapid same-trader/same-token events."""

    def __init__(
        self,
        emit_fn: EmitCallback,
        idle_timeout_sec: float = 30.0,
        max_hold_sec: float = 120.0,
    ):
        """
        Args:
            emit_fn: ``async def emit(alert, count) -> None`` — called once
                per consolidated batch. ``count`` is the number of raw
                alerts that merged into the batch (always >= 1).
            idle_timeout_sec: flush the batch if no new alert has arrived
                in this many seconds. Default 30s.
            max_hold_sec: flush the batch regardless of activity once its
                age exceeds this. Default 120s. Prevents a constantly-
                active trader from holding a batch indefinitely.
        """
        self._emit = emit_fn
        self._idle = float(idle_timeout_sec)
        self._max_hold = float(max_hold_sec)
        self._pending: dict[tuple[str, str, str], _Batch] = {}
        self._tasks: dict[tuple[str, str, str], asyncio.Task] = {}

    async def add(self, alert: WalletTrackerAlert) -> None:
        """Ingest one parsed alert. Either starts a new batch or
        accumulates into an existing one."""
        if not alert.parsed_ok:
            # Debouncer only handles well-parsed alerts. Callers should
            # fall back to the direct dispatch path for anything else.
            return

        key = self._make_key(alert)
        now = time.monotonic()

        batch = self._pending.get(key)
        if batch is not None:
            batch.accumulate(alert, now)
            logger.info(
                "Debouncer accumulated: %s/%s/%s (count=%d)",
                alert.action, alert.token, alert.trader, len(batch.alerts),
            )
            return

        # New batch
        batch = _Batch(
            first_alert=alert,
            latest_alert=alert,
            alerts=[alert],
            first_seen=now,
            last_seen=now,
        )
        self._pending[key] = batch
        self._tasks[key] = asyncio.create_task(
            self._flush_after(key),
            name=f"debouncer-{alert.action}-{alert.token}-{alert.trader}",
        )
        logger.info(
            "Debouncer started: %s/%s/%s",
            alert.action, alert.token, alert.trader,
        )

    def _make_key(
        self, alert: WalletTrackerAlert,
    ) -> tuple[str, str, str]:
        return (
            (alert.trader or "").lower(),
            (alert.token or "").lower(),
            (alert.action or "").upper(),
        )

    async def _flush_after(self, key: tuple[str, str, str]) -> None:
        """Background task: polls every ``idle_timeout_sec`` and flushes
        when the batch has been idle long enough OR hit the max hold."""
        try:
            while True:
                await asyncio.sleep(self._idle)
                batch = self._pending.get(key)
                if batch is None:
                    return  # already flushed / cancelled
                now = time.monotonic()
                idle_time = now - batch.last_seen
                total_time = now - batch.first_seen
                if idle_time >= self._idle or total_time >= self._max_hold:
                    # Flush
                    flushed = self._pending.pop(key, None)
                    self._tasks.pop(key, None)
                    if flushed is None:
                        return
                    try:
                        consolidated, count = flushed.to_consolidated()
                        logger.info(
                            "Debouncer flush: %s/%s/%s count=%d idle=%.1fs span=%.1fs",
                            consolidated.action,
                            consolidated.token,
                            consolidated.trader,
                            count,
                            idle_time,
                            total_time,
                        )
                        await self._emit(consolidated, count)
                    except Exception:
                        logger.exception(
                            "Debouncer emit failed for %s", key,
                        )
                    return
                # else: loop, new alerts arrived, keep waiting
        except asyncio.CancelledError:
            # Graceful shutdown; emit what we have
            batch = self._pending.pop(key, None)
            self._tasks.pop(key, None)
            if batch is not None:
                try:
                    consolidated, count = batch.to_consolidated()
                    await self._emit(consolidated, count)
                except Exception:
                    logger.exception(
                        "Debouncer shutdown emit failed for %s", key,
                    )
            raise

    async def stop(self) -> None:
        """Cancel all flush tasks, emitting any in-flight batches first.
        Safe to call multiple times."""
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._pending.clear()
        self._tasks.clear()
