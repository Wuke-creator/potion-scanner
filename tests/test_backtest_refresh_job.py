"""Tests for the nightly backtest refresh job: picks the wallets whose
copier verdict is missing or ageing out, runs them through the real
BacktestRunner contract (faked), and defers to a manual /backtest.

The verdict writeback itself is the runner's job (and is covered by
test_backtest_runner.py), so these tests assert selection, deference,
scheduling and summarisation only.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.config.settings import BacktestConfig
from src.trading.backtest.refresh_job import BacktestRefreshJob
from src.trading.backtest.runner import PRIMARY_DELAY_SEC
from src.trading.wallet_metrics_db import TrackedWallet, WalletMetricsDB

TRACKED_STALE = "0xadd12adbbd5db87674b38af99b6dd34dd2a45e0d"
TRACKED_FRESH = "0x2f7b0d0c259f599536037b9c6c782c04a2aec71d"
CANDIDATE = "0x1f7b0d0c259f599536037b9c6c782c04a2aec71d"

DAY = 86_400


@pytest_asyncio.fixture
async def metrics(tmp_path: Path):
    db = WalletMetricsDB(db_path=str(tmp_path / "scout.db"))
    await db.open()
    yield db
    await db.close()


def _cfg(**kw) -> BacktestConfig:
    defaults = dict(
        refresh_enabled=True, refresh_hour_utc=4, refresh_days=60,
        refresh_max_age_days=21, job_timeout_min=30,
    )
    defaults.update(kw)
    return BacktestConfig(**defaults)


def _runner(result=None, *, hang=False):
    """Stand-in for BacktestRunner.run(spec, progress)."""
    seen: dict = {}

    async def run(spec, progress):
        seen["addresses"] = list(spec.addresses)
        seen["days"] = spec.days
        seen["label"] = spec.label
        await progress("working")
        if hang:
            await asyncio.sleep(3600)
        return result if result is not None else SimpleNamespace(wallets=[])

    return SimpleNamespace(run=run), seen


def _wallet_result(address, *, net=None, error=""):
    return SimpleNamespace(
        address=address, error=error,
        net_by_delay={} if net is None else {PRIMARY_DELAY_SEC: net},
    )


# --- wallet selection -----------------------------------------------------

@pytest.mark.asyncio
async def test_selects_wallets_with_no_verdict(metrics):
    await metrics.save_tracked_wallet(
        TrackedWallet(address=TRACKED_STALE, status="tracked"),
    )
    job = BacktestRefreshJob(
        runner=_runner()[0], metrics_db=metrics, backtest_cfg=_cfg(),
    )
    assert await job.due_addresses() == [TRACKED_STALE]


@pytest.mark.asyncio
async def test_skips_wallets_with_a_fresh_verdict(metrics):
    await metrics.save_tracked_wallet(
        TrackedWallet(address=TRACKED_FRESH, status="tracked"),
    )
    await metrics.record_backtest_fitness(
        TRACKED_FRESH, latency_ratio=0.8, copier_net=1234.0,
    )
    job = BacktestRefreshJob(
        runner=_runner()[0], metrics_db=metrics, backtest_cfg=_cfg(),
    )
    assert await job.due_addresses() == []


@pytest.mark.asyncio
async def test_selects_wallets_whose_verdict_aged_out(metrics):
    await metrics.save_tracked_wallet(
        TrackedWallet(address=TRACKED_STALE, status="tracked"),
    )
    await metrics.record_backtest_fitness(
        TRACKED_STALE, latency_ratio=0.8, copier_net=10.0,
    )
    job = BacktestRefreshJob(
        runner=_runner()[0], metrics_db=metrics, backtest_cfg=_cfg(),
    )
    # 21-day threshold: nothing due now, everything due 22 days from now
    assert await job.due_addresses() == []
    assert await job.due_addresses(now=time.time() + 22 * DAY) == [TRACKED_STALE]


@pytest.mark.asyncio
async def test_candidates_excluded_unless_configured(metrics):
    await metrics.save_tracked_wallet(
        TrackedWallet(address=CANDIDATE, status="candidate"),
    )
    tracked_only = BacktestRefreshJob(
        runner=_runner()[0], metrics_db=metrics, backtest_cfg=_cfg(),
    )
    assert await tracked_only.due_addresses() == []

    with_candidates = BacktestRefreshJob(
        runner=_runner()[0], metrics_db=metrics,
        backtest_cfg=_cfg(refresh_include_candidates=True),
    )
    assert await with_candidates.due_addresses() == [CANDIDATE]


# --- run_once -------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_once_passes_configured_window_and_summarises(metrics):
    await metrics.save_tracked_wallet(
        TrackedWallet(address=TRACKED_STALE, status="tracked"),
    )
    runner, seen = _runner(SimpleNamespace(wallets=[
        _wallet_result(TRACKED_STALE, net=-250.5),
    ]))
    job = BacktestRefreshJob(
        runner=runner, metrics_db=metrics, backtest_cfg=_cfg(refresh_days=45),
    )
    summary = await job.run_once()

    assert seen["addresses"] == [TRACKED_STALE]
    assert seen["days"] == 45
    assert summary["requested"] == 1
    assert summary["completed"] == 1
    assert summary["errors"] == 0
    assert summary["verdicts"][TRACKED_STALE] == -250.5
    assert summary["skipped"] == ""


@pytest.mark.asyncio
async def test_run_once_counts_failed_wallets_without_raising(metrics):
    await metrics.save_tracked_wallet(
        TrackedWallet(address=TRACKED_STALE, status="tracked"),
    )
    await metrics.save_tracked_wallet(
        TrackedWallet(address=TRACKED_FRESH, status="tracked"),
    )
    runner, _ = _runner(SimpleNamespace(wallets=[
        _wallet_result(TRACKED_STALE, net=12.0),
        _wallet_result(TRACKED_FRESH, error="no fills in window"),
    ]))
    job = BacktestRefreshJob(
        runner=runner, metrics_db=metrics, backtest_cfg=_cfg(),
    )
    summary = await job.run_once()

    assert summary["completed"] == 1
    assert summary["errors"] == 1
    assert TRACKED_FRESH not in summary["verdicts"]


@pytest.mark.asyncio
async def test_run_once_noops_when_nothing_is_due(metrics):
    runner, seen = _runner()
    job = BacktestRefreshJob(
        runner=runner, metrics_db=metrics, backtest_cfg=_cfg(),
    )
    summary = await job.run_once()

    assert summary["requested"] == 0
    assert "fresh" in summary["skipped"]
    assert "addresses" not in seen      # runner never invoked


@pytest.mark.asyncio
async def test_defers_to_a_running_manual_backtest(metrics):
    await metrics.save_tracked_wallet(
        TrackedWallet(address=TRACKED_STALE, status="tracked"),
    )
    runner, seen = _runner()
    job = BacktestRefreshJob(
        runner=runner, metrics_db=metrics, backtest_cfg=_cfg(),
        busy=lambda: True,
    )
    summary = await job.run_once()

    assert "manual" in summary["skipped"]
    assert "addresses" not in seen      # runner never invoked


@pytest.mark.asyncio
async def test_timeout_is_contained_and_clears_running_flag(metrics):
    await metrics.save_tracked_wallet(
        TrackedWallet(address=TRACKED_STALE, status="tracked"),
    )
    runner, _ = _runner(hang=True)
    job = BacktestRefreshJob(
        runner=runner, metrics_db=metrics, backtest_cfg=_cfg(job_timeout_min=0),
    )
    summary = await job.run_once()

    assert "timed out" in summary["skipped"]
    assert job.running is False


# --- scheduling -----------------------------------------------------------

def test_next_run_is_within_a_day(metrics):
    job = BacktestRefreshJob(
        runner=_runner()[0], metrics_db=metrics, backtest_cfg=_cfg(),
    )
    secs = job._seconds_until_next_run()
    assert 60 < secs <= 86_400 + 60


def test_manual_command_exposes_a_busy_predicate():
    """The wiring in main.py depends on this property existing."""
    from src.trading.backtest.commands import BacktestCommands

    cmds = BacktestCommands(
        config=SimpleNamespace(), runner=AsyncMock(), metrics_db=AsyncMock(),
    )
    assert cmds.busy is False
