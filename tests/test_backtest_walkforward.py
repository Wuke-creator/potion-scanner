"""Tests for walk-forward scout validation: spearman helper, the
insufficient-archive guard, and a full multi-week loop over a synthetic
archive (real store, fake feed IO)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.config.settings import BacktestConfig, WalletCopyConfig
from src.trading.backtest.data_feed import BacktestDataFeed
from src.trading.backtest.data_store import BacktestStore
from src.trading.backtest.walkforward import (
    WalkForward,
    format_walkforward,
    spearman_rank,
)

DAY_MS = 86_400_000
GOOD = "0x" + "a" * 40


class TestSpearman:
    def test_perfect_positive(self):
        pairs = [(float(i), float(i * 2)) for i in range(6)]
        assert spearman_rank(pairs) == pytest.approx(1.0)

    def test_perfect_negative(self):
        pairs = [(float(i), float(-i)) for i in range(6)]
        assert spearman_rank(pairs) == pytest.approx(-1.0)

    def test_ties_handled(self):
        pairs = [(1.0, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 4.0), (4.0, 5.0)]
        rho = spearman_rank(pairs)
        assert rho is not None and 0.5 < rho <= 1.0

    def test_too_few_pairs(self):
        assert spearman_rank([(1.0, 1.0)] * 4) is None

    def test_constant_scores_none(self):
        assert spearman_rank([(5.0, float(i)) for i in range(6)]) is None


@pytest_asyncio.fixture
async def store(tmp_path: Path):
    s = BacktestStore(db_path=str(tmp_path / "cache.db"))
    await s.open()
    yield s
    await s.close()


def _wf(store, feed=None):
    return WalkForward(
        feed=feed or AsyncMock(spec=BacktestDataFeed),
        store=store,
        wallet_cfg=WalletCopyConfig(seed_addresses=()),
        backtest_cfg=BacktestConfig(),
    )


async def _noop_progress(_):
    pass


class TestInsufficientArchive:
    @pytest.mark.asyncio
    async def test_empty_archive_reports_not_possible(self, store):
        wf = _wf(store)
        result = await wf.run(_noop_progress)
        assert result.insufficient_archive
        text = format_walkforward(result)
        assert "NOT POSSIBLE YET" in text
        assert "survivorship" in text

    @pytest.mark.asyncio
    async def test_recent_snapshots_not_scoreable_yet(self, store):
        # snapshots exist but their following week hasn't finished
        today = time.strftime("%Y-%m-%d", time.gmtime())
        await store.save_leaderboard_snapshot(today, {"leaderboardRows": []},
                                              n_rows=0)
        wf = _wf(store)
        result = await wf.run(_noop_progress)
        assert result.insufficient_archive


def _board_row(addr, month_roi=0.2):
    return {
        "ethAddress": addr,
        "accountValue": "100000",
        "windowPerformances": [
            ["day", {"pnl": "100", "roi": "0.01", "vlm": "1000"}],
            ["week", {"pnl": "700", "roi": "0.03", "vlm": "10000"}],
            ["month", {"pnl": "3000", "roi": str(month_roi), "vlm": "100000"}],
            ["allTime", {"pnl": "20000", "roi": "0.9", "vlm": "1000000"}],
        ],
    }


def _round_trip_fills(t0, n, *, win=True, coin="A"):
    out = []
    hour = 3_600_000
    for i in range(n):
        base = t0 + i * DAY_MS
        out.append({
            "coin": coin, "time": base, "sz": "20", "side": "B", "px": "100",
            "startPosition": "0", "closedPnl": "0", "fee": "0", "tid": base,
        })
        out.append({
            "coin": coin, "time": base + 5 * hour, "sz": "20", "side": "A",
            "px": "101", "startPosition": "20",
            "closedPnl": "50" if win else "-40", "fee": "0", "tid": base + 1,
        })
    return out


class TestWalkLoop:
    @pytest.mark.asyncio
    async def test_multi_week_loop_selects_and_evaluates(self, store):
        # archive: 5 weekly snapshots, all ending well in the past
        now_ms = int(time.time() * 1000)
        first_ms = now_ms - 8 * 7 * DAY_MS
        dates = []
        for w in range(5):
            for d in range(7):
                ms = first_ms + (w * 7 + d) * DAY_MS
                date = time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))
                dates.append(date)
                await store.save_leaderboard_snapshot(
                    date, {"leaderboardRows": [_board_row(GOOD)]}, n_rows=1,
                )

        feed = AsyncMock()

        async def _fills(addr, *, start_ms, end_ms):
            # steady winner, active right up to the window end so the
            # as-of scorer never sees it as dormant
            span_days = max(1, (end_ms - start_ms) // DAY_MS)
            n = min(int(span_days), 20)
            return _round_trip_fills(end_ms - n * DAY_MS, n), True

        feed.fills = AsyncMock(side_effect=_fills)

        from src.trading.backtest.position_events import AccountValueCurve

        async def _curve(addr, fills):
            return AccountValueCurve(anchors=[], fills=[], fallback=100_000.0)

        feed.account_value_curve = AsyncMock(side_effect=_curve)

        from src.trading.backtest.simulator import MarketData

        async def _market(coins, *, start_ms, end_ms):
            return MarketData(candles_1m={}, candles_1h={}, funding={})

        feed.market_data = AsyncMock(side_effect=_market)

        wf = _wf(store, feed=feed)
        result = await wf.run(_noop_progress)
        assert not result.insufficient_archive
        assert len(result.weeks) >= 3
        # promotion needs the streak: first week no, later weeks yes
        assert result.weeks[0].promoted == []
        promoted_somewhere = any(w.promoted for w in result.weeks)
        assert promoted_somewhere
        text = format_walkforward(result)
        assert "out-of-sample" in text
