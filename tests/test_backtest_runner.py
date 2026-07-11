"""Tests for the data feed splice logic, the backtest runner end-to-end
(fake feed, real store), and the report formatter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.config.settings import BacktestConfig, WalletCopyConfig
from src.trading.backtest.data_feed import splice_candles
from src.trading.backtest.data_store import BacktestStore, Candle
from src.trading.backtest.report import format_report
from src.trading.backtest.runner import (
    BacktestRunner,
    BacktestSpec,
    PRIMARY_DELAY_SEC,
)
from src.trading.backtest.simulator import MarketData

T0 = 1_760_000_000_000
MIN = 60_000
HOUR = 3_600_000
ADDR = "0x" + "a" * 40


def _c(ts, o, h, l, c):  # noqa: E741
    return Candle(ts=ts, o=o, h=h, l=l, c=c)


class TestSpliceCandles:
    def test_no_coarse_needed_when_fine_full(self):
        fine = [_c(T0 + i * MIN, 1, 1, 1, 1) for i in range(15)]
        coarse = [_c(T0, 1, 1, 1, 1)]
        merged, spans = splice_candles(fine, coarse, coarse_step_ms=15 * MIN)
        assert merged == fine and spans == []

    def test_coarse_fills_gap_and_reports_span(self):
        fine = [_c(T0 + i * MIN, 1, 1, 1, 1) for i in range(15)]
        gap_coarse = [
            _c(T0 + 15 * MIN, 1, 1, 1, 1),
            _c(T0 + 30 * MIN, 1, 1, 1, 1),
        ]
        merged, spans = splice_candles(
            fine, gap_coarse, coarse_step_ms=15 * MIN,
        )
        assert len(merged) == 17
        assert spans == [(T0 + 15 * MIN, T0 + 45 * MIN)]  # adjacent merged

    def test_merged_sorted(self):
        fine = [_c(T0 + 20 * MIN, 1, 1, 1, 1)]
        coarse = [_c(T0, 1, 1, 1, 1)]
        merged, _ = splice_candles(fine, coarse, coarse_step_ms=15 * MIN)
        assert [c.ts for c in merged] == sorted(c.ts for c in merged)


def _fill(coin, t, sz, side, px, start=None, closed=0.0):
    f = {"coin": coin, "time": t, "sz": str(sz), "side": side, "px": str(px),
         "closedPnl": str(closed), "fee": "0", "tid": t}
    if start is not None:
        f["startPosition"] = str(start)
    return f


def _fake_feed(*, fills, candles_1m, candles_1h):
    feed = AsyncMock()
    feed.fills = AsyncMock(return_value=(fills, True))

    async def _curve(address, f):
        from src.trading.backtest.position_events import AccountValueCurve

        return AccountValueCurve(anchors=[], fills=[], fallback=1_000.0)

    feed.account_value_curve = AsyncMock(side_effect=_curve)

    async def _market(coins, *, start_ms, end_ms):
        return MarketData(
            candles_1m={c: candles_1m for c in coins},
            candles_1h={c: candles_1h for c in coins},
            funding={c: [] for c in coins},
        )

    feed.market_data = AsyncMock(side_effect=_market)
    return feed


@pytest_asyncio.fixture
async def store(tmp_path: Path):
    s = BacktestStore(db_path=str(tmp_path / "cache.db"))
    await s.open()
    yield s
    await s.close()


def _runner(store, feed, blofin=None):
    return BacktestRunner(
        feed=feed, store=store,
        wallet_cfg=WalletCopyConfig(seed_addresses=()),
        backtest_cfg=BacktestConfig(),
        blofin_client=blofin,
    )


def _scenario():
    """One leader open (60% of $1k account) then close 3h later; flat
    1m candles so the ladder never fills and the leader exit closes us."""
    fills = [
        _fill("HYPE", T0, 24.0, "B", 25.0, start=0.0),        # $600 notional
        _fill("HYPE", T0 + 3 * HOUR, 24.0, "A", 25.4, closed=9.6),
    ]
    candles_1m = [
        _c(T0 + i * MIN, 25.0 + i * 0.001, 25.1 + i * 0.001,
           24.9 + i * 0.001, 25.05 + i * 0.001)
        for i in range(5 * 60)
    ]
    candles_1h = [
        _c(T0 - 40 * HOUR + i * HOUR, 25, 25.5, 24.5, 25) for i in range(40)
    ]
    return fills, candles_1m, candles_1h


class TestRunnerEndToEnd:
    @pytest.mark.asyncio
    async def test_produces_populated_result(self, store):
        fills, c1m, c1h = _scenario()
        runner = _runner(store, _fake_feed(
            fills=fills, candles_1m=c1m, candles_1h=c1h,
        ))
        notes = []

        async def progress(text):
            notes.append(text)

        result = await runner.run(
            BacktestSpec(addresses=[ADDR], days=7), progress,
        )
        assert len(result.wallets) == 1
        wr = result.wallets[0]
        assert wr.error == ""
        assert wr.n_open_events == 1
        assert result.pooled is not None and result.pooled.n == 1
        # delay curve has every grid delay plus the primary
        assert PRIMARY_DELAY_SEC in result.net_by_delay
        assert len(result.net_by_delay) >= 4
        # plateau has all three mults, floor sensitivity all three floors
        assert [m for m, _, _ in result.plateau] == [1.0, 1.5, 2.0]
        assert set(result.floor_sensitivity) == {0.15, 0.25, 0.40}
        assert notes  # progress got called

    @pytest.mark.asyncio
    async def test_persists_run_and_trades(self, store):
        fills, c1m, c1h = _scenario()
        runner = _runner(store, _fake_feed(
            fills=fills, candles_1m=c1m, candles_1h=c1h,
        ))

        async def progress(_):
            pass

        result = await runner.run(
            BacktestSpec(addresses=[ADDR], days=7), progress,
        )
        cur = await store._conn.execute(
            "SELECT COUNT(*) AS n FROM backtest_runs WHERE run_id=?",
            (result.run_id,),
        )
        assert (await cur.fetchone())["n"] == 1
        cur = await store._conn.execute(
            "SELECT COUNT(*) AS n FROM backtest_trades WHERE run_id=?",
            (result.run_id,),
        )
        assert (await cur.fetchone())["n"] == 1

    @pytest.mark.asyncio
    async def test_wallet_error_isolated(self, store):
        feed = AsyncMock()
        feed.fills = AsyncMock(side_effect=RuntimeError("api down"))
        runner = _runner(store, feed)

        async def progress(_):
            pass

        result = await runner.run(
            BacktestSpec(addresses=[ADDR], days=7), progress,
        )
        assert result.wallets[0].error
        assert result.pooled is not None and result.pooled.n == 0

    @pytest.mark.asyncio
    async def test_no_fills_reports_reason(self, store):
        feed = AsyncMock()
        feed.fills = AsyncMock(return_value=([], True))
        runner = _runner(store, feed)

        async def progress(_):
            pass

        result = await runner.run(
            BacktestSpec(addresses=[ADDR], days=7), progress,
        )
        assert result.wallets[0].error == "no fills in window"

    @pytest.mark.asyncio
    async def test_unlisted_coins_filtered(self, store):
        fills, c1m, c1h = _scenario()
        blofin = AsyncMock()
        blofin.resolve_inst_id = AsyncMock(return_value=None)
        runner = _runner(store, _fake_feed(
            fills=fills, candles_1m=c1m, candles_1h=c1h,
        ), blofin=blofin)

        async def progress(_):
            pass

        result = await runner.run(
            BacktestSpec(addresses=[ADDR], days=7), progress,
        )
        wr = result.wallets[0]
        assert wr.unmapped_coins == ["HYPE"]
        assert wr.error == "no copyable events"


class TestReport:
    @pytest.mark.asyncio
    async def test_report_formats_and_chunks(self, store):
        fills, c1m, c1h = _scenario()
        runner = _runner(store, _fake_feed(
            fills=fills, candles_1m=c1m, candles_1h=c1h,
        ))

        async def progress(_):
            pass

        result = await runner.run(
            BacktestSpec(addresses=[ADDR], days=7), progress,
        )
        chunks = format_report(result)
        assert chunks
        assert all(len(c) <= 3800 for c in chunks)
        text = "\n".join(chunks)
        assert "VERDICT" in text
        assert "hop" in text
        assert "Caveats" in text
        assert "HAND-PICKED" in text
        assert result.run_id in text
