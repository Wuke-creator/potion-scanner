"""Tests for the /backtest Telegram command surface: gating, target
resolution, the single-job lock, cancellation and progress plumbing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.config.settings import AutotradeConfig, Config, WalletCopyConfig, BacktestConfig
from src.trading.backtest.commands import BacktestCommands, BacktestJobManager
from src.trading.wallet_metrics_db import TrackedWallet, WalletMetricsDB

UID = 8406521671
SEED = ("0x" + "1" * 40, "0x" + "2" * 40)


@pytest_asyncio.fixture
async def metrics_db(tmp_path: Path):
    db = WalletMetricsDB(db_path=str(tmp_path / "scout.db"))
    await db.open()
    yield db
    await db.close()


def _config():
    cfg = Config()
    cfg.autotrade = AutotradeConfig(allowlist=frozenset({UID}))
    cfg.wallet_copy = WalletCopyConfig(seed_addresses=SEED)
    cfg.backtest = BacktestConfig(job_timeout_min=1)
    return cfg


def _commands(metrics_db, runner=None):
    return BacktestCommands(
        config=_config(),
        runner=runner or AsyncMock(),
        metrics_db=metrics_db,
    )


def _update(uid=UID):
    reply = AsyncMock(return_value=SimpleNamespace(message_id=555))
    msg = SimpleNamespace(reply_text=reply, chat_id=999)
    return SimpleNamespace(
        effective_message=msg, effective_user=SimpleNamespace(id=uid),
    )


def _context(args, *, bot=None):
    loop_tasks = []

    def create_task(coro, name=None):
        t = asyncio.get_event_loop().create_task(coro)
        loop_tasks.append(t)
        return t

    return SimpleNamespace(
        args=args,
        application=SimpleNamespace(create_task=create_task),
        bot=bot or AsyncMock(),
        _tasks=loop_tasks,
    )


class TestGatesAndParsing:
    @pytest.mark.asyncio
    async def test_non_allowlisted_ignored(self, metrics_db):
        cmds = _commands(metrics_db)
        upd = _update(uid=12345)
        await cmds._cmd(upd, _context([]))
        upd.effective_message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_args_shows_help(self, metrics_db):
        cmds = _commands(metrics_db)
        upd = _update()
        await cmds._cmd(upd, _context([]))
        text = upd.effective_message.reply_text.call_args.args[0]
        assert "/backtest" in text and "cancel" in text

    @pytest.mark.asyncio
    async def test_bad_days_rejected(self, metrics_db):
        cmds = _commands(metrics_db)
        upd = _update()
        await cmds._cmd(upd, _context(["0x" + "a" * 40, "soon"]))
        assert "number" in upd.effective_message.reply_text.call_args.args[0]

    @pytest.mark.asyncio
    async def test_cancel_without_job(self, metrics_db):
        cmds = _commands(metrics_db)
        upd = _update()
        await cmds._cmd(upd, _context(["cancel"]))
        assert "No backtest" in upd.effective_message.reply_text.call_args.args[0]


class TestAddressResolution:
    @pytest.mark.asyncio
    async def test_explicit_address_lowercased(self, metrics_db):
        cmds = _commands(metrics_db)
        out = await cmds._resolve_addresses("0x" + "A" * 40)
        assert out == ["0x" + "a" * 40]

    @pytest.mark.asyncio
    async def test_tracked_falls_back_to_seeds_when_empty(self, metrics_db):
        cmds = _commands(metrics_db)
        assert await cmds._resolve_addresses("tracked") == list(SEED)

    @pytest.mark.asyncio
    async def test_tracked_uses_db_when_present(self, metrics_db):
        await metrics_db.save_tracked_wallet(
            TrackedWallet(address="0xdb", status="tracked"),
        )
        cmds = _commands(metrics_db)
        assert await cmds._resolve_addresses("tracked") == ["0xdb"]

    @pytest.mark.asyncio
    async def test_garbage_target(self, metrics_db):
        cmds = _commands(metrics_db)
        assert await cmds._resolve_addresses("wen lambo") == []


class TestJobLifecycle:
    @pytest.mark.asyncio
    async def test_job_runs_and_reports(self, metrics_db):
        runner = AsyncMock()
        from src.trading.backtest.runner import RunResult, BacktestSpec

        runner.run = AsyncMock(return_value=RunResult(
            run_id="bt-test", spec=BacktestSpec(addresses=["0x1"], days=7),
        ))
        cmds = _commands(metrics_db, runner=runner)
        upd = _update()
        ctx = _context(["0x" + "a" * 40, "7"])
        await cmds._cmd(upd, ctx)
        assert ctx._tasks, "job task was created"
        await asyncio.gather(*ctx._tasks)
        runner.run.assert_awaited_once()
        sent = [c.kwargs.get("text", "") for c in ctx.bot.send_message.call_args_list]
        assert any("VERDICT" in t for t in sent)

    @pytest.mark.asyncio
    async def test_second_job_blocked_while_running(self, metrics_db):
        runner = AsyncMock()
        release = asyncio.Event()

        async def slow_run(spec, progress):
            await release.wait()
            from src.trading.backtest.runner import RunResult

            return RunResult(run_id="bt-slow", spec=spec)

        runner.run = slow_run
        cmds = _commands(metrics_db, runner=runner)
        ctx = _context(["0x" + "a" * 40])
        upd1 = _update()
        await cmds._cmd(upd1, ctx)
        upd2 = _update()
        await cmds._cmd(upd2, _context(["0x" + "b" * 40]))
        assert "already running" in upd2.effective_message.reply_text.call_args.args[0]
        release.set()
        await asyncio.gather(*ctx._tasks)

    @pytest.mark.asyncio
    async def test_cancel_running_job(self, metrics_db):
        runner = AsyncMock()
        started = asyncio.Event()

        async def hang(spec, progress):
            started.set()
            await asyncio.sleep(3600)

        runner.run = hang
        cmds = _commands(metrics_db, runner=runner)
        ctx = _context(["0x" + "a" * 40])
        await cmds._cmd(_update(), ctx)
        await started.wait()
        upd = _update()
        await cmds._cmd(upd, _context(["cancel"], bot=ctx.bot))
        assert "Cancelling" in upd.effective_message.reply_text.call_args.args[0]
        await asyncio.gather(*ctx._tasks, return_exceptions=True)
        assert not cmds._manager.running
        sent = [c.kwargs.get("text", "") for c in ctx.bot.send_message.call_args_list]
        assert any("cancelled" in t for t in sent)


class TestJobManager:
    @pytest.mark.asyncio
    async def test_status_and_locking(self):
        mgr = BacktestJobManager()
        assert not mgr.running and mgr.status == ""

        release = asyncio.Event()

        async def work():
            await release.wait()

        app = SimpleNamespace(
            create_task=lambda coro, name=None: asyncio.get_event_loop().create_task(coro),
        )
        assert mgr.try_start(app, work(), label="test 7d", owner=1)
        assert mgr.running and "test 7d" in mgr.status
        second = work()
        assert not mgr.try_start(app, second, label="other", owner=2)
        second.close()   # un-awaited coroutine cleanup
        release.set()
        await asyncio.sleep(0.01)
        assert not mgr.running
