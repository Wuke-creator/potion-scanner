"""Telegram /backtest command (allowlist-gated, single job at a time).

  /backtest                          status + help
  /backtest <0xaddr> [days]          backtest one wallet
  /backtest tracked [days]           backtest the tracked set
  /backtest all [days]               tracked + candidates
  /backtest walkforward              point-in-time scout validation
  /backtest cancel                   cancel the running job

Runs take minutes: the handler spawns the job through the application's
own task tracking (so shutdown cancels it cleanly), edits ONE status
message with throttled progress, and DMs the final report in chunks.
Backtests place no orders and touch no keys; they read public history.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from src.config import Config
from src.trading.backtest.report import format_report
from src.trading.backtest.runner import BacktestRunner, BacktestSpec
from src.trading.wallet_metrics_db import WalletMetricsDB

logger = logging.getLogger(__name__)

_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_PROGRESS_MIN_INTERVAL_SEC = 3.0
_MAX_DAYS = 180
_DEFAULT_DAYS = 60

_HELP = (
    "Backtest a wallet (or the tracked set) under our copy rules.\n\n"
    "/backtest 0x... [days]   one wallet (default 60 days)\n"
    "/backtest tracked [days] the tracked set\n"
    "/backtest all [days]     tracked + candidates\n"
    "/backtest walkforward    point-in-time scout validation\n"
    "/backtest cancel         stop the running job\n\n"
    "Simulates the COPIER's PnL: confirm-delay grid, realistic/pessimistic "
    "fill bounds, ATR-ladder vs mirror vs hop exits, fees + funding. "
    "No orders are placed."
)


class BacktestJobManager:
    """One backtest at a time, cancellable, with owner + age introspection."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._label = ""
        self._owner = 0
        self._started = 0.0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def status(self) -> str:
        if not self.running:
            return ""
        return (
            f"{self._label} (started {int(time.time() - self._started)}s "
            f"ago by {self._owner})"
        )

    def try_start(self, application: Application, coro, *, label: str,
                  owner: int) -> bool:
        if self.running:
            coro.close()   # never-scheduled coroutine must be closed
            return False
        self._label, self._owner, self._started = label, owner, time.time()
        self._task = application.create_task(coro, name="backtest_job")
        return True

    def cancel(self) -> bool:
        if not self.running:
            return False
        assert self._task is not None
        self._task.cancel()
        return True


class BacktestCommands:
    def __init__(
        self,
        *,
        config: Config,
        runner: BacktestRunner,
        metrics_db: WalletMetricsDB,
        walkforward=None,          # WalkForward; optional until wired
    ):
        self._config = config
        self._runner = runner
        self._metrics_db = metrics_db
        self._walkforward = walkforward
        self._manager = BacktestJobManager()

    @property
    def busy(self) -> bool:
        """True while a manual /backtest job is in flight. The scheduled
        refresh job reads this to skip a night rather than run alongside."""
        return self._manager.running

    def register(self, application: Application) -> None:
        application.add_handler(CommandHandler("backtest", self._cmd))

    async def _cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None:
            return
        if user.id not in self._config.autotrade.allowlist:
            return
        args = list(context.args or [])

        if not args:
            status = self._manager.status
            text = (f"Running: {status}\n\n" if status else "") + _HELP
            await msg.reply_text(text)
            return

        if args[0].lower() == "cancel":
            if self._manager.cancel():
                await msg.reply_text("Cancelling the running backtest.")
            else:
                await msg.reply_text("No backtest is running.")
            return

        if args[0].lower() == "walkforward":
            if self._walkforward is None:
                await msg.reply_text("Walk-forward is not wired up.")
                return
            status_msg = await msg.reply_text(
                "Walk-forward validation started (needs an aged snapshot "
                "archive; report follows).",
            )
            started = self._manager.try_start(
                context.application,
                self._run_walkforward_job(
                    context, chat_id=msg.chat_id,
                    status_message_id=status_msg.message_id,
                ),
                label="walkforward", owner=user.id,
            )
            if not started:
                await msg.reply_text(
                    f"A backtest is already running: {self._manager.status}.",
                )
            return

        # target + days
        target = args[0]
        days = _DEFAULT_DAYS
        if len(args) > 1:
            try:
                days = max(1, min(_MAX_DAYS, int(args[1])))
            except ValueError:
                await msg.reply_text(f"Days must be a number, got {args[1]!r}.")
                return

        addresses = await self._resolve_addresses(target)
        if not addresses:
            await msg.reply_text(
                "Nothing to backtest. Give a 0x wallet address, 'tracked' "
                "or 'all' (no wallets are tracked yet: the scout is dark, "
                "seeds appear once it runs).",
            )
            return

        label = f"{target} {days}d"
        status_msg = await msg.reply_text(
            f"Backtest queued: {len(addresses)} wallet(s), {days} days. "
            f"This runs for a few minutes; I'll update here.",
        )
        started = self._manager.try_start(
            context.application,
            self._run_job(
                context, chat_id=msg.chat_id, status_message_id=status_msg.message_id,
                addresses=addresses, days=days, label=label,
            ),
            label=label, owner=user.id,
        )
        if not started:
            await msg.reply_text(
                f"A backtest is already running: {self._manager.status}. "
                "Use /backtest cancel first.",
            )

    async def _resolve_addresses(self, target: str) -> list[str]:
        t = target.strip()
        if _ADDR_RE.match(t):
            return [t.lower()]
        kind = t.lower()
        if kind == "tracked":
            wallets = await self._metrics_db.list_wallets(status="tracked")
        elif kind == "all":
            wallets = await self._metrics_db.list_wallets()
        else:
            return []
        addresses = [w.address for w in wallets]
        if not addresses and kind in ("tracked", "all"):
            # scout hasn't seeded yet: fall back to the configured seeds so
            # the backtester is usable before anything is flipped on
            addresses = list(self._config.wallet_copy.seed_addresses)
        return addresses

    async def _run_job(
        self, context: ContextTypes.DEFAULT_TYPE, *, chat_id: int,
        status_message_id: int, addresses: list[str], days: int, label: str,
    ) -> None:
        bot = context.bot
        last_edit = 0.0
        pending: list[str] = []

        async def progress(text: str) -> None:
            nonlocal last_edit
            pending.append(text)
            now = time.monotonic()
            if now - last_edit < _PROGRESS_MIN_INTERVAL_SEC:
                return
            last_edit = now
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=status_message_id,
                    text=f"Backtest {label}\n" + pending[-1],
                )
            except Exception:  # noqa: BLE001 - stale/unchanged edits are fine
                try:
                    await bot.send_message(chat_id=chat_id, text=pending[-1])
                except Exception:  # noqa: BLE001
                    logger.warning("backtest progress DM failed")

        timeout = self._config.backtest.job_timeout_min * 60
        try:
            result = await asyncio.wait_for(
                self._runner.run(
                    BacktestSpec(addresses=addresses, days=days, label=label),
                    progress,
                ),
                timeout=timeout,
            )
        except asyncio.CancelledError:
            await self._safe_send(bot, chat_id, f"Backtest {label} cancelled.")
            raise
        except asyncio.TimeoutError:
            await self._safe_send(
                bot, chat_id,
                f"Backtest {label} timed out after "
                f"{self._config.backtest.job_timeout_min} min. Try fewer "
                "wallets or a shorter window.",
            )
            return
        except Exception:  # noqa: BLE001
            logger.exception("backtest job crashed")
            await self._safe_send(
                bot, chat_id,
                f"Backtest {label} crashed; check the logs.",
            )
            return

        for chunk in format_report(result):
            await self._safe_send(bot, chat_id, chunk)

    async def _run_walkforward_job(
        self, context: ContextTypes.DEFAULT_TYPE, *, chat_id: int,
        status_message_id: int,
    ) -> None:
        from src.trading.backtest.walkforward import format_walkforward

        bot = context.bot
        last_edit = 0.0

        async def progress(text: str) -> None:
            nonlocal last_edit
            now = time.monotonic()
            if now - last_edit < _PROGRESS_MIN_INTERVAL_SEC:
                return
            last_edit = now
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=status_message_id,
                    text=f"Walk-forward\n{text}",
                )
            except Exception:  # noqa: BLE001
                pass

        timeout = self._config.backtest.job_timeout_min * 60
        try:
            result = await asyncio.wait_for(
                self._walkforward.run(progress), timeout=timeout,
            )
        except asyncio.CancelledError:
            await self._safe_send(bot, chat_id, "Walk-forward cancelled.")
            raise
        except asyncio.TimeoutError:
            await self._safe_send(bot, chat_id, "Walk-forward timed out.")
            return
        except Exception:  # noqa: BLE001
            logger.exception("walkforward job crashed")
            await self._safe_send(bot, chat_id,
                                  "Walk-forward crashed; check the logs.")
            return
        await self._safe_send(bot, chat_id, format_walkforward(result))

    @staticmethod
    async def _safe_send(bot, chat_id: int, text: str) -> None:
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except Exception:  # noqa: BLE001
            logger.warning("backtest DM failed", exc_info=True)
