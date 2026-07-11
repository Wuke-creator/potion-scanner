"""Tests for the backtest archive store (candles, funding, fills,
leaderboard snapshots, retention)."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from src.trading.backtest.data_store import BacktestStore, interval_ms

DAY_MS = 86_400_000
NOW_MS = 1_760_000_000_000


@pytest_asyncio.fixture
async def store(tmp_path: Path):
    s = BacktestStore(db_path=str(tmp_path / "backtest_cache.db"))
    await s.open()
    yield s
    await s.close()


def _candle_rows(t0: int, n: int, step: int = 60_000):
    return [
        {"t": t0 + i * step, "o": "100", "h": "101", "l": "99",
         "c": "100.5", "v": "5"}
        for i in range(n)
    ]


class TestCandles:
    @pytest.mark.asyncio
    async def test_roundtrip_and_latest(self, store):
        n = await store.upsert_candles("HYPE", "1m", _candle_rows(NOW_MS, 10))
        assert n == 10
        assert await store.latest_candle_ts("HYPE", "1m") == NOW_MS + 9 * 60_000
        out = await store.get_candles(
            "HYPE", "1m", start_ms=NOW_MS, end_ms=NOW_MS + 4 * 60_000,
        )
        assert len(out) == 5
        assert out[0].o == 100.0 and out[0].h == 101.0

    @pytest.mark.asyncio
    async def test_upsert_idempotent(self, store):
        rows = _candle_rows(NOW_MS, 5)
        await store.upsert_candles("HYPE", "1m", rows)
        await store.upsert_candles("HYPE", "1m", rows)
        out = await store.get_candles(
            "HYPE", "1m", start_ms=0, end_ms=NOW_MS + DAY_MS,
        )
        assert len(out) == 5

    @pytest.mark.asyncio
    async def test_drops_in_progress_candle(self, store):
        rows = _candle_rows(NOW_MS, 3)
        # "now" lands mid-way through the third candle
        now = NOW_MS + 2 * 60_000 + 30_000
        n = await store.upsert_candles(
            "HYPE", "1m", rows, drop_open_after_ms=now,
        )
        assert n == 2

    @pytest.mark.asyncio
    async def test_coverage_fraction(self, store):
        await store.upsert_candles("HYPE", "1m", _candle_rows(NOW_MS, 30))
        full = await store.candle_coverage(
            "HYPE", "1m", start_ms=NOW_MS, end_ms=NOW_MS + 30 * 60_000,
        )
        empty = await store.candle_coverage(
            "ZEC", "1m", start_ms=NOW_MS, end_ms=NOW_MS + 30 * 60_000,
        )
        assert full == 1.0
        assert empty == 0.0

    def test_interval_ms(self):
        assert interval_ms("1m") == 60_000
        assert interval_ms("1h") == 3_600_000
        assert interval_ms("bogus") == 3_600_000


class TestFundingAndFills:
    @pytest.mark.asyncio
    async def test_funding_roundtrip(self, store):
        rows = [
            {"time": NOW_MS + i * 3_600_000, "fundingRate": "0.0001",
             "premium": "0.0002"}
            for i in range(4)
        ]
        assert await store.upsert_funding("HYPE", rows) == 4
        assert await store.latest_funding_ts("HYPE") == NOW_MS + 3 * 3_600_000
        out = await store.get_funding(
            "HYPE", start_ms=NOW_MS, end_ms=NOW_MS + DAY_MS,
        )
        assert len(out) == 4 and out[0][1] == pytest.approx(0.0001)

    @pytest.mark.asyncio
    async def test_fills_roundtrip_and_coverage(self, store):
        fills = [
            {"tid": i, "time": NOW_MS + i, "coin": "HYPE", "px": "25",
             "sz": "1", "side": "B"}
            for i in range(6)
        ]
        assert await store.upsert_fills("0xABC", fills) == 6
        # address lookups are case-insensitive (stored lowercased)
        out = await store.get_fills("0xabc", start_ms=NOW_MS, end_ms=NOW_MS + 10)
        assert len(out) == 6 and out[0]["px"] == "25"
        await store.add_fills_coverage(
            "0xABC", start_ms=NOW_MS, end_ms=NOW_MS + 10, complete=False,
        )
        cov = await store.fills_coverage("0xabc")
        assert cov == [(NOW_MS, NOW_MS + 10, False)]

    @pytest.mark.asyncio
    async def test_fills_without_tid_skipped(self, store):
        assert await store.upsert_fills("0x1", [{"time": 1, "coin": "A"}]) == 0


class TestLeaderboardArchive:
    @pytest.mark.asyncio
    async def test_snapshot_roundtrip_compressed(self, store):
        body = {"leaderboardRows": [{"ethAddress": "0x1"}] * 50}
        await store.save_leaderboard_snapshot("2026-07-11", body, n_rows=50)
        assert await store.has_leaderboard_snapshot("2026-07-11")
        assert not await store.has_leaderboard_snapshot("2026-07-10")
        loaded = await store.load_leaderboard_snapshot("2026-07-11")
        assert loaded == body
        assert await store.list_leaderboard_snapshot_dates() == ["2026-07-11"]

    @pytest.mark.asyncio
    async def test_account_values_series(self, store):
        await store.upsert_leaderboard_accounts("2026-07-10", {"0xA": 1000.0})
        await store.upsert_leaderboard_accounts("2026-07-11", {"0xA": 1100.0})
        series = await store.account_values("0xa")
        assert series == [("2026-07-10", 1000.0), ("2026-07-11", 1100.0)]

    @pytest.mark.asyncio
    async def test_wallet_state_snapshot(self, store):
        await store.save_wallet_state(
            "0xA", "2026-07-11", account_value=5000.0,
            raw_body={"marginSummary": {"accountValue": "5000"}},
        )
        # write is idempotent per (address, date)
        await store.save_wallet_state(
            "0xA", "2026-07-11", account_value=5001.0, raw_body={},
        )


class TestRunsAndPrune:
    @pytest.mark.asyncio
    async def test_run_log_roundtrip(self, store):
        await store.save_backtest_run(
            "run1", spec={"wallet": "0x1"}, params={"grid": 1}, summary={"n": 3},
        )
        await store.save_backtest_trades("run1", "p0", [{"r": 1.5}, {"r": -1.0}])

    @pytest.mark.asyncio
    async def test_prune_respects_retention(self, store):
        old = NOW_MS - 200 * DAY_MS
        await store.upsert_candles("A", "1m", _candle_rows(old, 3))
        await store.upsert_candles("A", "1m", _candle_rows(NOW_MS, 3))
        await store.upsert_candles("A", "1h", _candle_rows(old, 3, 3_600_000))
        await store.upsert_fills("0x1", [
            {"tid": 1, "time": old, "coin": "A"},
            {"tid": 2, "time": NOW_MS, "coin": "A"},
        ])
        await store.prune(
            now_ms=NOW_MS, candle_1m_keep_days=90, fills_keep_days=180,
        )
        rem_1m = await store.get_candles("A", "1m", start_ms=0, end_ms=NOW_MS * 2)
        rem_1h = await store.get_candles("A", "1h", start_ms=0, end_ms=NOW_MS * 2)
        assert len(rem_1m) == 3          # old 1m pruned
        assert len(rem_1h) == 3          # 1h kept forever
        fills = await store.get_fills("0x1", start_ms=0, end_ms=NOW_MS * 2)
        assert [f["tid"] for f in fills] == [2]
