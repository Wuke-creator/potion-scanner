"""Unit tests for PnLMonitor — PnL threshold alerts.

Tests:
- Profit threshold detection (+5%)
- Loss threshold detection (-3%)
- Deduplication (no duplicate alerts)
- Alert clearing when PnL drops back below threshold
- Alert clearing when position disappears
- Paused pipelines are skipped
"""

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from src.telegram.pnl_monitor import PnLMonitor, PROFIT_THRESHOLD_PCT, LOSS_THRESHOLD_PCT


def _make_orchestrator(positions, paused=False, has_notifier=True):
    """Build a mock orchestrator with one pipeline."""
    notifier = AsyncMock() if has_notifier else None

    pipeline = MagicMock()
    pipeline._notifier = notifier

    client = MagicMock()
    client.get_open_positions.return_value = positions

    ctx = MagicMock()
    ctx.client = client
    ctx.pipeline = pipeline
    ctx.paused = paused

    orchestrator = MagicMock()
    orchestrator.pipelines = {"user-1": ctx}

    return orchestrator, notifier


def _pos(coin="BTC", entry_px=100.0, size=1.0, unrealized_pnl=0.0):
    return {
        "coin": coin,
        "entryPx": str(entry_px),
        "szi": str(size),
        "size": size,
        "unrealizedPnl": str(unrealized_pnl),
    }


@pytest.mark.asyncio
async def test_profit_threshold_alert():
    """Position at +6% triggers a profit alert."""
    # entry=100, size=1 => notional=100, unrealized=6 => 6%
    orch, notifier = _make_orchestrator([_pos(unrealized_pnl=6.0)])
    monitor = PnLMonitor(orchestrator=orch, user_db=MagicMock())

    result = await monitor.check_positions()

    assert len(result["alerts"]) == 1
    assert result["alerts"][0] == ("user-1", "BTC", "profit", 6.0)
    notifier.notify_pnl_alert.assert_awaited_once_with("BTC", "LONG", 6.0, "profit")


@pytest.mark.asyncio
async def test_loss_threshold_alert():
    """Position at -4% triggers a loss alert."""
    orch, notifier = _make_orchestrator([_pos(unrealized_pnl=-4.0)])
    monitor = PnLMonitor(orchestrator=orch, user_db=MagicMock())

    result = await monitor.check_positions()

    assert len(result["alerts"]) == 1
    assert result["alerts"][0] == ("user-1", "BTC", "loss", -4.0)
    notifier.notify_pnl_alert.assert_awaited_once_with("BTC", "LONG", -4.0, "loss")


@pytest.mark.asyncio
async def test_no_alert_within_thresholds():
    """Position at +2% triggers neither alert."""
    orch, notifier = _make_orchestrator([_pos(unrealized_pnl=2.0)])
    monitor = PnLMonitor(orchestrator=orch, user_db=MagicMock())

    result = await monitor.check_positions()

    assert result["alerts"] == []
    notifier.notify_pnl_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_deduplication():
    """Same position above threshold on second tick does not re-alert."""
    orch, notifier = _make_orchestrator([_pos(unrealized_pnl=6.0)])
    monitor = PnLMonitor(orchestrator=orch, user_db=MagicMock())

    await monitor.check_positions()
    notifier.reset_mock()

    result = await monitor.check_positions()

    assert result["alerts"] == []
    notifier.notify_pnl_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_alert_clears_when_pnl_drops():
    """Alert clears when PnL drops below threshold, allowing re-alert."""
    orch, notifier = _make_orchestrator([_pos(unrealized_pnl=6.0)])
    monitor = PnLMonitor(orchestrator=orch, user_db=MagicMock())

    # First tick: alert
    await monitor.check_positions()
    notifier.reset_mock()

    # PnL drops to +2%
    orch.pipelines["user-1"].client.get_open_positions.return_value = [_pos(unrealized_pnl=2.0)]
    result = await monitor.check_positions()
    assert ("user-1", "BTC", "profit") in [tuple(c) for c in result["cleared"]]

    # PnL goes back to +7% — should re-alert
    orch.pipelines["user-1"].client.get_open_positions.return_value = [_pos(unrealized_pnl=7.0)]
    result = await monitor.check_positions()
    assert len(result["alerts"]) == 1
    notifier.notify_pnl_alert.assert_awaited_once()


@pytest.mark.asyncio
async def test_alert_clears_when_position_disappears():
    """Alert clears when position no longer exists."""
    orch, notifier = _make_orchestrator([_pos(unrealized_pnl=6.0)])
    monitor = PnLMonitor(orchestrator=orch, user_db=MagicMock())

    await monitor.check_positions()

    # Position gone
    orch.pipelines["user-1"].client.get_open_positions.return_value = []
    result = await monitor.check_positions()
    assert len(result["cleared"]) >= 1


@pytest.mark.asyncio
async def test_paused_pipeline_skipped():
    """Paused pipelines are not checked."""
    orch, notifier = _make_orchestrator([_pos(unrealized_pnl=10.0)], paused=True)
    monitor = PnLMonitor(orchestrator=orch, user_db=MagicMock())

    result = await monitor.check_positions()

    assert result["alerts"] == []
    notifier.notify_pnl_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_short_position_detected():
    """Short position (negative size) is identified correctly."""
    orch, notifier = _make_orchestrator([_pos(size=-1.0, unrealized_pnl=-4.0)])
    monitor = PnLMonitor(orchestrator=orch, user_db=MagicMock())

    result = await monitor.check_positions()

    assert result["alerts"][0][2] == "loss"
    notifier.notify_pnl_alert.assert_awaited_once_with("BTC", "SHORT", -4.0, "loss")
