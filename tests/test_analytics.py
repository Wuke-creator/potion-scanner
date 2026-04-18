"""Tests for src/analytics/db.py — signal + PnL tracking."""

from pathlib import Path

import pytest
import pytest_asyncio

from src.analytics.db import AnalyticsDB


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    d = AnalyticsDB(db_path=str(tmp_path / "analytics.db"))
    await d.open()
    yield d
    await d.close()


@pytest.mark.asyncio
class TestRecordSignal:
    async def test_record_new_signal(self, db: AnalyticsDB):
        await db.record_signal(
            trade_id=1001, channel_key="perp_bot", pair="ETH/USDT",
            side="LONG", entry=3200.0, leverage=10,
        )
        counts = await db.count_signals_per_channel(since_epoch=0)
        assert counts == {"perp_bot": 1}

    async def test_duplicate_signal_is_idempotent(self, db: AnalyticsDB):
        await db.record_signal(1001, "perp_bot", "ETH/USDT", "LONG", 3200.0, 10)
        await db.record_signal(1001, "perp_bot", "ETH/USDT", "LONG", 3200.0, 10)
        counts = await db.count_signals_per_channel(since_epoch=0)
        assert counts == {"perp_bot": 1}

    async def test_same_trade_id_across_channels_is_separate(self, db: AnalyticsDB):
        await db.record_signal(1001, "perp_bot", "ETH/USDT", "LONG", 3200.0, 10)
        await db.record_signal(1001, "manual_perp", "BTC/USDT", "SHORT", 68000.0, 5)
        counts = await db.count_signals_per_channel(since_epoch=0)
        assert counts == {"perp_bot": 1, "manual_perp": 1}


@pytest.mark.asyncio
class TestTopPnL:
    async def test_returns_highest_pnl_per_channel(self, db: AnalyticsDB):
        # Perp bot: two trades, second has higher PnL
        await db.record_signal(1, "perp_bot", "ETH/USDT", "LONG", 3200.0, 10)
        await db.record_event(1, "perp_bot", "tp_hit", tp_number=1, pnl_pct=25.0)
        await db.record_signal(2, "perp_bot", "SOL/USDT", "LONG", 140.0, 15)
        await db.record_event(2, "perp_bot", "all_tp_hit", tp_number=3, pnl_pct=180.0)
        # Prediction: one trade
        await db.record_signal(3, "prediction", "PEPE/USDT", "LONG", 0.000012, 5)
        await db.record_event(3, "prediction", "tp_hit", tp_number=2, pnl_pct=420.0)

        tops = await db.top_pnl_per_channel(since_epoch=0)
        assert tops["perp_bot"].pair == "SOL/USDT"
        assert tops["perp_bot"].pnl_pct == pytest.approx(180.0)
        assert tops["perp_bot"].opened_at > 0
        assert tops["prediction"].pnl_pct == pytest.approx(420.0)

    async def test_stop_hits_ignored_from_top_pnl(self, db: AnalyticsDB):
        await db.record_signal(1, "perp_bot", "ETH/USDT", "LONG", 3200.0, 10)
        await db.record_event(1, "perp_bot", "stop_hit", pnl_pct=-77.0)
        # A negative stop-hit should NOT surface as "top PnL"
        tops = await db.top_pnl_per_channel(since_epoch=0)
        assert "perp_bot" not in tops

    async def test_empty_db_returns_empty_dict(self, db: AnalyticsDB):
        assert await db.top_pnl_per_channel(since_epoch=0) == {}


@pytest.mark.asyncio
class TestStatsWindow:
    async def test_stats_window_fills_zero_for_missing_channels(self, db: AnalyticsDB):
        await db.record_signal(1, "perp_bot", "ETH/USDT", "LONG", 3200.0, 10)
        window = await db.stats_window(
            days=7, label="7d",
            channel_keys=["perp_bot", "manual_perp", "prediction"],
        )
        assert window.window_label == "7d"
        assert window.per_channel["perp_bot"].signal_count == 1
        assert window.per_channel["manual_perp"].signal_count == 0
        assert window.per_channel["prediction"].signal_count == 0
        assert window.per_channel["manual_perp"].top_pnl is None
