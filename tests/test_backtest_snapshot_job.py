"""Tests for the nightly snapshot job: archives leaderboard + candles +
funding + fills + tracked wallet states into a real BacktestStore (tmp),
with all Hyperliquid IO faked."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.config.settings import BacktestConfig, WalletCopyConfig
from src.trading.backtest.data_store import BacktestStore
from src.trading.backtest.snapshot_job import SnapshotJob, candle_gap_check
from src.trading.wallet_metrics_db import TrackedWallet, WalletMetricsDB

TRACKED = "0xadd12adbbd5db87674b38af99b6dd34dd2a45e0d"
CANDIDATE = "0x1f7b0d0c259f599536037b9c6c782c04a2aec71d"
# Recent timestamp so the end-of-run retention prune never eats fixture rows.
RECENT_MS = int(time.time() * 1000) - 3_600_000


def _board_row(addr, account="120000"):
    return {
        "ethAddress": addr,
        "accountValue": account,
        "windowPerformances": [
            ["day", {"pnl": "100", "roi": "0.01", "vlm": "1000"}],
            ["week", {"pnl": "500", "roi": "0.02", "vlm": "5000"}],
            ["month", {"pnl": "2000", "roi": "0.1", "vlm": "40000"}],
            ["allTime", {"pnl": "9000", "roi": "0.5", "vlm": "200000"}],
        ],
    }


@pytest_asyncio.fixture
async def dbs(tmp_path: Path):
    store = BacktestStore(db_path=str(tmp_path / "cache.db"))
    await store.open()
    metrics = WalletMetricsDB(db_path=str(tmp_path / "scout.db"))
    await metrics.open()
    await metrics.save_tracked_wallet(TrackedWallet(address=TRACKED, status="tracked"))
    await metrics.save_tracked_wallet(TrackedWallet(address=CANDIDATE, status="candidate"))
    yield store, metrics
    await metrics.close()
    await store.close()


def _make_job(store, metrics, *, board=None, fills=None, candles=None):
    info = AsyncMock()
    info.get_leaderboard_raw = AsyncMock(return_value=(
        board if board is not None
        else {"leaderboardRows": [_board_row(TRACKED), _board_row("0x" + "9" * 40)]}
    ))
    info.get_all_user_fills = AsyncMock(return_value=(
        fills if fills is not None else (
            [{"tid": 1, "time": RECENT_MS, "coin": "HYPE",
              "px": "25", "sz": "10", "side": "B", "startPosition": "0"}],
            True,
        )
    ))
    info.get_candles = AsyncMock(return_value=(
        candles if candles is not None else [
            {"t": RECENT_MS, "o": "1", "h": "2", "l": "0.5", "c": "1.5"},
        ]
    ))
    info.get_funding_history = AsyncMock(return_value=[
        {"time": RECENT_MS, "fundingRate": "0.0001"},
    ])
    info.get_clearinghouse_state_raw = AsyncMock(return_value={
        "marginSummary": {"accountValue": "120000"}, "assetPositions": [],
    })
    job = SnapshotJob(
        info_client=info, store=store, metrics_db=metrics,
        wallet_cfg=WalletCopyConfig(seed_addresses=()),
        backtest_cfg=BacktestConfig(max_snapshot_coins=10),
        pace_sec=0,
    )
    return job, info


class TestSnapshotRunOnce:
    @pytest.mark.asyncio
    async def test_archives_everything(self, dbs):
        store, metrics = dbs
        job, info = _make_job(store, metrics)
        summary = await job.run_once()

        assert summary["leaderboard_rows"] == 2
        dates = await store.list_leaderboard_snapshot_dates()
        assert len(dates) == 1
        # tracked wallet's account value became a daily anchor
        assert await store.account_values(TRACKED)
        # fills cached for both known wallets
        assert summary["fills"] == 2
        assert await store.get_fills(TRACKED, start_ms=0, end_ms=2**62)
        # coin list = HYPE (from fills) + majors, all candled at 3 intervals
        assert summary["coins"] == 4
        assert info.get_candles.await_count == 4 * 3
        # funding per coin, tracked state archived
        assert summary["funding"] == 4
        assert summary["states"] == 1

    @pytest.mark.asyncio
    async def test_leaderboard_failure_does_not_kill_run(self, dbs):
        from src.trading.hl_info_client import HLInfoError

        store, metrics = dbs
        job, info = _make_job(store, metrics)
        info.get_leaderboard_raw = AsyncMock(side_effect=HLInfoError("down"))
        summary = await job.run_once()
        assert summary["leaderboard_rows"] == 0
        assert summary["states"] == 1     # rest of the pass still ran

    @pytest.mark.asyncio
    async def test_incomplete_fills_marked_in_coverage(self, dbs):
        store, metrics = dbs
        job, _ = _make_job(
            store, metrics,
            fills=([{"tid": 5, "time": RECENT_MS, "coin": "ZEC"}], False),
        )
        await job.run_once()
        cov = await store.fills_coverage(TRACKED)
        assert len(cov) == 1 and cov[0][2] is False

    @pytest.mark.asyncio
    async def test_second_run_same_day_is_idempotent(self, dbs):
        store, metrics = dbs
        job, _ = _make_job(store, metrics)
        await job.run_once()
        await job.run_once()
        assert len(await store.list_leaderboard_snapshot_dates()) == 1
        series = await store.account_values(TRACKED)
        assert len(series) == 1           # same-day anchor overwritten, not duped

    @pytest.mark.asyncio
    async def test_candle_fetch_uses_overlap_window(self, dbs):
        store, metrics = dbs
        # pre-seed a latest candle so the next fetch starts from overlap
        import time as _time

        now_ms = int(_time.time() * 1000)
        await store.upsert_candles("BTC", "1h", [
            {"t": now_ms - 3_600_000, "o": "1", "h": "1", "l": "1", "c": "1"},
        ])
        job, info = _make_job(store, metrics)
        await job.run_once()
        btc_1h_calls = [
            c for c in info.get_candles.await_args_list
            if c.args[0] == "BTC" and c.kwargs.get("interval") == "1h"
        ]
        assert btc_1h_calls
        start = btc_1h_calls[0].kwargs["start_ms"]
        # started at latest - 2h overlap, not at the full 48h lookback
        assert start == now_ms - 3_600_000 - 2 * 3_600_000


class TestGapCheck:
    def test_no_prior_data_is_not_a_gap(self):
        assert candle_gap_check(None, 10**15, "1m") is False

    def test_gap_beyond_upstream_retention(self):
        now = 10**15
        assert candle_gap_check(now - 5001 * 60_000, now, "1m") is True
        assert candle_gap_check(now - 4000 * 60_000, now, "1m") is False
