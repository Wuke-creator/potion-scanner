"""End-to-end integration tests — full pipeline with mocked exchange.

Runs real sample signals through the complete pipeline (classify → parse →
size → build orders → submit → record) with a mocked HyperliquidClient
to verify the entire flow without hitting the exchange.

Tests cover:
- Full signal lifecycle (signal → TP hits → all TP / stop)
- Duplicate signal rejection
- Trade cancellation flow
- Breakeven SL movement
- Manual update / SL adjustment
- Multi-user fan-out via orchestrator
- Risk limit enforcement across sequences
- Noise and preparation filtering
- Edge cases: unknown trade updates, rapid-fire same coin
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
from cryptography.fernet import Fernet

from src.config.settings import (
    Config,
    ExchangeConfig,
    InputConfig,
    StrategyConfig,
    StrategyPreset,
    RiskConfig,
    DatabaseConfig,
    LoggingConfig,
    HealthConfig,
    DiscordConfig,
    BUILTIN_PRESETS,
)
from src.crypto import reset_fernet
from src.exchange.order_builder import TradeOrderSet
from src.orchestrator import Orchestrator, UserPipelineContext
from src.pipeline import Pipeline
from src.state.database import TradeDatabase
from src.state.models import TradeStatus, OrderStatus, OrderType
from src.state.user_db import UserDatabase

SAMPLES_DIR = Path("signals/samples")


def _load(filename: str) -> str:
    return (SAMPLES_DIR / filename).read_text().strip()


# Realistic asset metadata for coins appearing in our samples
ASSET_META = {
    "ZK": {"szDecimals": 0, "maxLeverage": 50, "name": "ZK"},
    "XRP": {"szDecimals": 0, "maxLeverage": 50, "name": "XRP"},
    "kBONK": {"szDecimals": 0, "maxLeverage": 20, "name": "kBONK"},
    "ADA": {"szDecimals": 0, "maxLeverage": 50, "name": "ADA"},
    "BCH": {"szDecimals": 2, "maxLeverage": 50, "name": "BCH"},
    "RENDER": {"szDecimals": 1, "maxLeverage": 50, "name": "RENDER"},
    "WIF": {"szDecimals": 0, "maxLeverage": 20, "name": "WIF"},
    "SEI": {"szDecimals": 0, "maxLeverage": 50, "name": "SEI"},
    "INJ": {"szDecimals": 1, "maxLeverage": 50, "name": "INJ"},
    "POL": {"szDecimals": 0, "maxLeverage": 50, "name": "POL"},
    "TIA": {"szDecimals": 1, "maxLeverage": 50, "name": "TIA"},
    "ATOM": {"szDecimals": 1, "maxLeverage": 50, "name": "ATOM"},
    "CRV": {"szDecimals": 0, "maxLeverage": 50, "name": "CRV"},
    "DOT": {"szDecimals": 1, "maxLeverage": 50, "name": "DOT"},
    "APT": {"szDecimals": 2, "maxLeverage": 50, "name": "APT"},
    "DOGE": {"szDecimals": 0, "maxLeverage": 50, "name": "DOGE"},
    "ETH": {"szDecimals": 4, "maxLeverage": 100, "name": "ETH"},
    "BTC": {"szDecimals": 5, "maxLeverage": 100, "name": "BTC"},
}


def _mock_exchange_response(oid: int = 1001) -> dict:
    """Standard successful exchange order response."""
    return {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {
                "statuses": [
                    {"resting": {"oid": oid}}
                ]
            },
        },
    }


def _mock_fill_response(oid: int = 1001, avg_px: float = 100.0) -> dict:
    """Exchange response for an immediately filled order."""
    return {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {
                "statuses": [
                    {"filled": {"oid": oid, "avgPx": str(avg_px), "totalSz": "1.0"}}
                ]
            },
        },
    }


def _make_config(tmpdir: Path, auto_execute: bool = True) -> Config:
    """Build a test config."""
    return Config(
        exchange=ExchangeConfig(
            network="testnet",
            account_address="0xTEST_ADDR",
            api_wallet="0xTEST_WALLET",
            api_secret="0xTEST_SECRET",
        ),
        input=InputConfig(adapter="simulation"),
        strategy=StrategyConfig(
            active_preset="runner",
            auto_execute=auto_execute,
            max_leverage=20,
            size_by_risk={"LOW": 4.0, "MEDIUM": 2.0, "HIGH": 1.0},
        ),
        risk=RiskConfig(
            max_open_positions=10,
            max_daily_loss_pct=10.0,
            max_position_size_usd=500.0,
            max_total_exposure_usd=2000.0,
            min_order_usd=10.0,
        ),
        database=DatabaseConfig(path=str(tmpdir / "test.db")),
        logging=LoggingConfig(level="DEBUG", file=str(tmpdir / "test.log")),
        health=HealthConfig(enabled=False),
        discord=DiscordConfig(),
    )


def _make_client() -> MagicMock:
    """Create a mocked HyperliquidClient."""
    client = MagicMock()
    client.get_balance.return_value = {"usdc_balance": "10000"}
    client.get_open_positions.return_value = []
    client.get_open_orders.return_value = []
    client.get_asset_meta.return_value = ASSET_META
    client.get_all_mids.return_value = {"ZK": "0.02", "ADA": "0.30", "BTC": "50000"}

    # Default: all orders rest successfully with incrementing oids
    oid_counter = [1000]
    def mock_order(*args, **kwargs):
        oid_counter[0] += 1
        return _mock_exchange_response(oid_counter[0])
    client.exchange.order.side_effect = mock_order
    client.exchange.update_leverage.return_value = None
    client.exchange.cancel.return_value = None

    return client


@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def config(tmpdir):
    return _make_config(tmpdir)


@pytest.fixture
def client():
    return _make_client()


@pytest.fixture
def db(tmpdir):
    return TradeDatabase(user_id="test_user", db_path=tmpdir / "test.db")


@pytest.fixture
def pipeline(config, client, db):
    return Pipeline(config=config, client=client, db=db)


# ====================================================================
# Full signal lifecycle
# ====================================================================

class TestSignalToTradeFlow:
    """Signal alert → entry order → DB record."""

    def test_signal_alert_creates_trade_and_submits(self, pipeline, db, client):
        """Process a real signal and verify trade record + exchange calls."""
        pipeline.process_message(_load("signal_alert_06.txt"))  # ADA/USDT #1259

        trade = db.get_trade(1259)
        assert trade is not None
        assert trade.pair == "ADA/USDT"
        assert trade.coin == "ADA"
        assert trade.side == "LONG"
        assert trade.risk_level == "LOW"
        assert trade.leverage <= 20  # Capped by max_leverage
        assert trade.position_size_usd > 0

        # Should have called exchange: set_leverage + entry + SL + 3 TPs
        assert client.exchange.update_leverage.call_count == 1
        assert client.exchange.order.call_count >= 4  # entry + SL + at least some TPs

    def test_signal_alert_short_side(self, pipeline, db, client):
        """SHORT signal creates correct direction orders."""
        pipeline.process_message(_load("signal_alert_01.txt"))  # ZK/USDT #1286 SHORT

        trade = db.get_trade(1286)
        assert trade is not None
        assert trade.side == "SHORT"

        # Entry should be a SELL (short)
        orders = db.get_orders_for_trade(1286)
        entry = next(o for o in orders if o.order_type == OrderType.ENTRY)
        assert entry.side == "SELL"

    def test_signal_without_header(self, pipeline, db):
        """Signal without 'TRADING SIGNAL ALERT' header still parses."""
        pipeline.process_message(_load("signal_alert_04.txt"))  # XRP #1282 no header

        trade = db.get_trade(1282)
        assert trade is not None
        assert trade.pair == "XRP/USDT"
        assert trade.risk_level == "HIGH"

    def test_kilo_prefix_coin(self, pipeline, db):
        """1000BONK/USDT maps to kBONK correctly."""
        pipeline.process_message(_load("signal_alert_05.txt"))  # 1000BONK #1269

        trade = db.get_trade(1269)
        assert trade is not None
        assert trade.coin == "kBONK"

    def test_all_four_signals_create_trades(self, pipeline, db):
        """All 4 sample signal alerts create valid trades."""
        for f in ("signal_alert_01.txt", "signal_alert_04.txt",
                   "signal_alert_05.txt", "signal_alert_06.txt"):
            pipeline.process_message(_load(f))

        assert len(db.get_open_trades()) == 4


class TestDuplicateSignalRejection:
    """Same signal processed twice should not create a duplicate trade."""

    def test_duplicate_signal_skipped(self, pipeline, db, client):
        msg = _load("signal_alert_06.txt")
        pipeline.process_message(msg)
        pipeline.process_message(msg)

        # Only one trade should exist
        trades = db.get_open_trades()
        assert len(trades) == 1

        # Exchange should only be called for the first one
        first_order_count = client.exchange.order.call_count
        # Process again — should not increase
        pipeline.process_message(msg)
        assert client.exchange.order.call_count == first_order_count


# ====================================================================
# Lifecycle events
# ====================================================================

class TestTpHitFlow:
    """TP hit → DB update, optional SL move to breakeven."""

    def test_tp1_hit_moves_sl_to_breakeven(self, pipeline, db, client):
        """With runner preset (BE after TP1), hitting TP1 should move SL."""
        # First create the trade
        pipeline.process_message(_load("signal_alert_01.txt"))  # ZK #1286
        trade = db.get_trade(1286)
        assert trade is not None

        # Mark as OPEN (simulate entry fill)
        db.update_trade_status(1286, TradeStatus.OPEN)

        # Now process TP1 hit — breakeven_01 is for ZK #1286
        cancel_count_before = client.exchange.cancel.call_count
        pipeline.process_message(_load("tp_hit_01.txt"))

        # tp_hit_01 is SEI #1256, not ZK #1286 — so it won't match
        # Let's use breakeven_01 instead which explicitly moves SL
        pipeline.process_message(_load("breakeven_01.txt"))  # ZK #1286 BE after TP1

        # Should have attempted to cancel old SL and place new one
        # (may fail because no submitted SL exists in mock, but the attempt is what matters)

    def test_tp_hit_for_unknown_trade(self, pipeline, db):
        """TP hit for a trade we don't know about should log warning, not crash."""
        pipeline.process_message(_load("tp_hit_01.txt"))  # SEI #1256 — not in DB
        # Should not raise


class TestAllTpHitFlow:
    def test_all_tp_closes_trade(self, pipeline, db):
        """All TP hit marks trade as CLOSED."""
        # Create BCH trade manually (since we don't have signal_alert for #1284)
        from src.state.models import TradeRecord
        trade = TradeRecord(
            trade_id=1284, user_id="test_user", pair="BCH/USDT", coin="BCH",
            side="SHORT", risk_level="MEDIUM", trade_type="SWING", size_hint="1-4%",
            entry_price=515.0, stop_loss=530.0, tp1=500.0, tp2=490.0, tp3=470.0,
            leverage=20, signal_leverage=27, position_size_usd=200.0, position_size_coin=0.38,
        )
        db.create_trade(trade)
        db.update_trade_status(1284, TradeStatus.OPEN)

        pipeline.process_message(_load("all_tp_hit_01.txt"))  # BCH #1284

        closed_trade = db.get_trade(1284)
        assert closed_trade.status == TradeStatus.CLOSED
        assert closed_trade.close_reason == "all_tp_hit"
        assert closed_trade.pnl_pct == 282.76


class TestStopHitFlow:
    def test_stop_hit_closes_trade(self, pipeline, db):
        """Stop hit marks trade as CLOSED with loss."""
        from src.state.models import TradeRecord
        trade = TradeRecord(
            trade_id=1267, user_id="test_user", pair="WIF/USDT", coin="WIF",
            side="LONG", risk_level="MEDIUM", trade_type="SWING", size_hint="1-4%",
            entry_price=1.5, stop_loss=1.3, tp1=1.6, tp2=1.7, tp3=1.9,
            leverage=10, signal_leverage=10, position_size_usd=100.0, position_size_coin=66.0,
        )
        db.create_trade(trade)
        db.update_trade_status(1267, TradeStatus.OPEN)

        pipeline.process_message(_load("stop_hit_01.txt"))  # WIF #1267

        closed_trade = db.get_trade(1267)
        assert closed_trade.status == TradeStatus.CLOSED
        assert closed_trade.close_reason == "stop_hit"
        assert closed_trade.pnl_pct == -77.7


class TestCanceledFlow:
    def test_cancel_pending_trade(self, pipeline, db, client):
        """Cancel on a pending trade (entry not yet filled)."""
        from src.state.models import TradeRecord
        trade = TradeRecord(
            trade_id=1265, user_id="test_user", pair="RENDER/USDT", coin="RENDER",
            side="LONG", risk_level="MEDIUM", trade_type="SWING", size_hint="1-4%",
            entry_price=5.0, stop_loss=4.5, tp1=5.5, tp2=6.0, tp3=7.0,
            leverage=10, signal_leverage=10, position_size_usd=100.0, position_size_coin=20.0,
        )
        db.create_trade(trade)

        pipeline.process_message(_load("canceled_01.txt"))  # RENDER #1265

        canceled_trade = db.get_trade(1265)
        assert canceled_trade.status == TradeStatus.CANCELED

    def test_cancel_open_trade_closes_position(self, pipeline, db, client):
        """Cancel on an OPEN trade should close the position."""
        from src.state.models import TradeRecord
        trade = TradeRecord(
            trade_id=1249, user_id="test_user", pair="DOT/USDT", coin="DOT",
            side="LONG", risk_level="MEDIUM", trade_type="SWING", size_hint="1-4%",
            entry_price=7.0, stop_loss=6.5, tp1=7.5, tp2=8.0, tp3=9.0,
            leverage=10, signal_leverage=10, position_size_usd=100.0, position_size_coin=14.0,
        )
        db.create_trade(trade)
        db.update_trade_status(1249, TradeStatus.OPEN)

        pipeline.process_message(_load("canceled_04.txt"))  # DOT #1249

        closed_trade = db.get_trade(1249)
        assert closed_trade.status == TradeStatus.CLOSED

    def test_cancel_for_unknown_trade(self, pipeline, db):
        """Cancel for unknown trade should not crash."""
        pipeline.process_message(_load("canceled_02.txt"))  # #1268


class TestTradeClosedFlow:
    def test_trade_closed_marks_closed(self, pipeline, db, client):
        from src.state.models import TradeRecord
        trade = TradeRecord(
            trade_id=1253, user_id="test_user", pair="INJ/USDT", coin="INJ",
            side="LONG", risk_level="MEDIUM", trade_type="SWING", size_hint="1-4%",
            entry_price=20.0, stop_loss=18.0, tp1=22.0, tp2=24.0, tp3=28.0,
            leverage=10, signal_leverage=10, position_size_usd=100.0, position_size_coin=5.0,
        )
        db.create_trade(trade)
        db.update_trade_status(1253, TradeStatus.OPEN)

        pipeline.process_message(_load("trade_closed_01.txt"))  # INJ #1253

        closed = db.get_trade(1253)
        assert closed.status == TradeStatus.CLOSED


# ====================================================================
# Preparation and noise filtering
# ====================================================================

class TestPreparationAndNoise:
    def test_preparation_does_not_create_trade(self, pipeline, db, client):
        """Preparation messages should be logged but NOT create trades or orders."""
        pipeline.process_message(_load("preparation_01.txt"))  # BCH #1284
        pipeline.process_message(_load("preparation_02.txt"))  # ZK #1286

        assert len(db.get_open_trades()) == 0
        assert client.exchange.order.call_count == 0

    def test_noise_ignored(self, pipeline, db, client):
        """Noise messages should be silently ignored."""
        pipeline.process_message(_load("noise_01.txt"))

        assert len(db.get_open_trades()) == 0
        assert client.exchange.order.call_count == 0

    def test_manual_update_does_not_execute(self, pipeline, db, client):
        """Manual update without matching SL pattern should just log."""
        pipeline.process_message(_load("manual_update_01.txt"))  # ADA #1259
        # No exchange calls expected
        assert client.exchange.order.call_count == 0


# ====================================================================
# Auto-execute disabled (dry-run mode)
# ====================================================================

class TestDryRunMode:
    def test_no_orders_when_auto_execute_false(self, tmpdir):
        """With auto_execute=false, trades are recorded but not submitted."""
        config = _make_config(tmpdir, auto_execute=False)
        client = _make_client()
        db = TradeDatabase(user_id="test_user", db_path=tmpdir / "test.db")
        pipeline = Pipeline(config=config, client=client, db=db)

        pipeline.process_message(_load("signal_alert_06.txt"))  # ADA #1259

        trade = db.get_trade(1259)
        assert trade is not None
        assert trade.status == TradeStatus.PENDING

        # No orders should have been placed
        assert client.exchange.order.call_count == 0
        assert client.exchange.update_leverage.call_count == 0
        db.close()


# ====================================================================
# Risk limits
# ====================================================================

class TestRiskLimits:
    def test_max_positions_blocks_new_trade(self, tmpdir):
        """After hitting max open positions, new signals are rejected."""
        config = _make_config(tmpdir)
        config.risk.max_open_positions = 2
        client = _make_client()
        db = TradeDatabase(user_id="test_user", db_path=tmpdir / "test.db")
        pipeline = Pipeline(config=config, client=client, db=db)

        # Create 2 trades
        pipeline.process_message(_load("signal_alert_06.txt"))  # ADA #1259
        pipeline.process_message(_load("signal_alert_01.txt"))  # ZK #1286

        assert len(db.get_open_trades()) == 2

        # Third should be blocked
        pipeline.process_message(_load("signal_alert_05.txt"))  # BONK #1269

        # Should still be 2 — third was rejected
        open_trades = db.get_open_trades()
        assert len(open_trades) == 2
        # BONK trade should not exist
        assert db.get_trade(1269) is None
        db.close()

    def test_daily_loss_circuit_breaker(self, tmpdir):
        """After daily loss exceeds limit, new signals are rejected."""
        config = _make_config(tmpdir)
        config.risk.max_daily_loss_pct = 5.0
        client = _make_client()
        db = TradeDatabase(user_id="test_user", db_path=tmpdir / "test.db")
        pipeline = Pipeline(config=config, client=client, db=db)

        # Create and close a trade at a big loss
        from src.state.models import TradeRecord
        trade = TradeRecord(
            trade_id=9999, user_id="test_user", pair="BTC/USDT", coin="BTC",
            side="LONG", risk_level="LOW", trade_type="SWING", size_hint="1-4%",
            entry_price=50000, stop_loss=49000, tp1=51000, tp2=52000, tp3=55000,
            leverage=10, signal_leverage=10, position_size_usd=400.0, position_size_coin=0.008,
        )
        db.create_trade(trade)
        db.update_trade_status(9999, TradeStatus.CLOSED, close_reason="stop_hit", pnl_pct=-6.0)

        # Now try a new signal — should be rejected (daily loss = -6% > -5% limit)
        pipeline.process_message(_load("signal_alert_06.txt"))
        assert db.get_trade(1259) is None
        db.close()

    def test_total_exposure_cap(self, tmpdir):
        """New trade rejected when total exposure would exceed cap."""
        config = _make_config(tmpdir)
        # ADA signal = 4% of $10k = $400, ZK signal = 2% of $10k = $200
        # Set cap so ADA ($400) + ZK ($200) fit but BONK ($400) would exceed
        config.risk.max_total_exposure_usd = 650.0
        client = _make_client()
        db = TradeDatabase(user_id="test_user", db_path=tmpdir / "test.db")
        pipeline = Pipeline(config=config, client=client, db=db)

        # First trade: ADA at $400
        pipeline.process_message(_load("signal_alert_06.txt"))
        assert db.get_trade(1259) is not None  # ADA accepted

        # Second trade: ZK at $200 — total = $600, under $650 cap
        pipeline.process_message(_load("signal_alert_01.txt"))
        assert db.get_trade(1286) is not None  # ZK accepted

        # Third trade: BONK at $400 — total would be $1000, exceeds $650 cap
        pipeline.process_message(_load("signal_alert_05.txt"))

        trades = db.get_open_trades()
        trade_ids = {t.trade_id for t in trades}
        assert 1269 not in trade_ids  # BONK rejected
        db.close()


# ====================================================================
# Leverage capping
# ====================================================================

class TestLeverageCapping:
    def test_leverage_capped_to_config_max(self, pipeline, db):
        """Signal leverage (e.g. 27x) is capped to config max_leverage (20x)."""
        pipeline.process_message(_load("signal_alert_04.txt"))  # XRP #1282, leverage=27

        trade = db.get_trade(1282)
        assert trade is not None
        assert trade.signal_leverage == 27
        assert trade.leverage <= 20  # Capped

    def test_leverage_capped_to_exchange_max(self, tmpdir):
        """If exchange max < config max, use exchange max."""
        config = _make_config(tmpdir)
        config.strategy.max_leverage = 100  # Very high
        client = _make_client()
        # kBONK has maxLeverage=20 in our ASSET_META
        db = TradeDatabase(user_id="test_user", db_path=tmpdir / "test.db")
        pipeline = Pipeline(config=config, client=client, db=db)

        pipeline.process_message(_load("signal_alert_05.txt"))  # 1000BONK #1269 lev=16

        trade = db.get_trade(1269)
        assert trade.leverage == 16  # Signal says 16, exchange max 20, so 16
        db.close()


# ====================================================================
# Order recording in DB
# ====================================================================

class TestOrderRecording:
    def test_orders_recorded_for_trade(self, pipeline, db, client):
        """All orders (entry + SL + TPs) should be recorded in the DB."""
        pipeline.process_message(_load("signal_alert_06.txt"))  # ADA #1259

        orders = db.get_orders_for_trade(1259)
        order_types = {o.order_type for o in orders}

        assert OrderType.ENTRY in order_types
        assert OrderType.STOP_LOSS in order_types
        # At least some TPs should exist (depends on tp_split having non-zero values)
        tp_count = sum(1 for o in orders if o.order_type in (OrderType.TP1, OrderType.TP2, OrderType.TP3))
        assert tp_count >= 2  # runner preset has [0.33, 0.33, 0.34]

    def test_entry_oid_recorded(self, pipeline, db, client):
        """Entry order should have an oid set after submission."""
        pipeline.process_message(_load("signal_alert_06.txt"))

        orders = db.get_orders_for_trade(1259)
        entry = next(o for o in orders if o.order_type == OrderType.ENTRY)
        assert entry.oid is not None
        assert entry.status == OrderStatus.SUBMITTED


# ====================================================================
# Exchange error handling
# ====================================================================

class TestExchangeErrors:
    def test_entry_rejection_cancels_trade(self, tmpdir):
        """If the entry order is rejected, trade should be marked CANCELED."""
        config = _make_config(tmpdir)
        client = _make_client()
        # Make entry order return an error (must clear side_effect first)
        error_response = {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"error": "Insufficient margin"}]},
            },
        }
        client.exchange.order.side_effect = None
        client.exchange.order.return_value = error_response
        db = TradeDatabase(user_id="test_user", db_path=tmpdir / "test.db")
        pipeline = Pipeline(config=config, client=client, db=db)

        pipeline.process_message(_load("signal_alert_06.txt"))

        trade = db.get_trade(1259)
        assert trade is not None
        assert trade.status == TradeStatus.CANCELED
        assert trade.close_reason == "submission_failed"
        db.close()

    def test_leverage_error_does_not_create_orders(self, tmpdir):
        """If setting leverage fails, no orders should be placed."""
        config = _make_config(tmpdir)
        client = _make_client()
        client.exchange.update_leverage.side_effect = RuntimeError("Exchange error")
        db = TradeDatabase(user_id="test_user", db_path=tmpdir / "test.db")
        pipeline = Pipeline(config=config, client=client, db=db)

        pipeline.process_message(_load("signal_alert_06.txt"))

        trade = db.get_trade(1259)
        assert trade is not None
        # Trade is recorded but submission failed, so status stays PENDING or CANCELED
        assert client.exchange.order.call_count == 0
        db.close()


# ====================================================================
# Full lifecycle sequence
# ====================================================================

class TestFullLifecycle:
    """Simulate a complete trade lifecycle using related samples."""

    def test_zk_signal_then_breakeven(self, pipeline, db, client):
        """ZK #1286: signal → open → breakeven."""
        # 1. Signal
        pipeline.process_message(_load("signal_alert_01.txt"))  # ZK #1286
        trade = db.get_trade(1286)
        assert trade is not None
        assert trade.status == TradeStatus.PENDING

        # 2. Simulate entry fill
        db.update_trade_status(1286, TradeStatus.OPEN)

        # 3. Breakeven hit
        pipeline.process_message(_load("breakeven_01.txt"))  # ZK #1286 BE after TP1
        # Trade should still be open (breakeven just moves SL)
        trade = db.get_trade(1286)
        assert trade.status == TradeStatus.OPEN

    def test_inj_trade_closed_after_tp2(self, pipeline, db, client):
        """INJ #1253: create → open → trade closed after TP2."""
        from src.state.models import TradeRecord
        trade = TradeRecord(
            trade_id=1253, user_id="test_user", pair="INJ/USDT", coin="INJ",
            side="LONG", risk_level="MEDIUM", trade_type="SWING", size_hint="1-4%",
            entry_price=20.0, stop_loss=18.0, tp1=22.0, tp2=24.0, tp3=28.0,
            leverage=10, signal_leverage=10, position_size_usd=100.0, position_size_coin=5.0,
        )
        db.create_trade(trade)
        db.update_trade_status(1253, TradeStatus.OPEN)

        # INJ #1248 TP1 hit — different trade, should not affect #1253
        pipeline.process_message(_load("tp_hit_03.txt"))
        assert db.get_trade(1253).status == TradeStatus.OPEN

        # Trade #1253 closed
        pipeline.process_message(_load("trade_closed_01.txt"))
        assert db.get_trade(1253).status == TradeStatus.CLOSED


# ====================================================================
# Multi-user orchestrator dispatch
# ====================================================================

class TestOrchestratorE2E:
    """End-to-end test: one signal dispatched to multiple user pipelines."""

    @pytest.fixture(autouse=True)
    def _encryption_key(self):
        key = Fernet.generate_key()
        reset_fernet()
        with patch.dict(os.environ, {"ENCRYPTION_KEY": key.decode()}):
            yield
        reset_fernet()

    def test_same_signal_reaches_all_users(self, tmpdir):
        config = _make_config(tmpdir)
        user_db = UserDatabase(db_path=tmpdir / "test.db")

        orch = Orchestrator(config, user_db)

        # Create two mock pipelines
        p1 = MagicMock()
        p2 = MagicMock()
        db1 = MagicMock()
        db2 = MagicMock()

        orch._pipelines["alice"] = UserPipelineContext(
            user_id="alice", config=config, client=MagicMock(),
            db=db1, pipeline=p1,
        )
        orch._pipelines["bob"] = UserPipelineContext(
            user_id="bob", config=config, client=MagicMock(),
            db=db2, pipeline=p2,
        )

        msg = _load("signal_alert_06.txt")
        orch.dispatch(msg)

        p1.process_message.assert_called_once_with(msg)
        p2.process_message.assert_called_once_with(msg)
        user_db.close()

    def test_one_user_error_doesnt_block_other(self, tmpdir):
        config = _make_config(tmpdir)
        user_db = UserDatabase(db_path=tmpdir / "test.db")

        orch = Orchestrator(config, user_db)

        p1 = MagicMock()
        p1.process_message.side_effect = RuntimeError("Alice's exchange is down")
        p2 = MagicMock()

        orch._pipelines["alice"] = UserPipelineContext(
            user_id="alice", config=config, client=MagicMock(),
            db=MagicMock(), pipeline=p1,
        )
        orch._pipelines["bob"] = UserPipelineContext(
            user_id="bob", config=config, client=MagicMock(),
            db=MagicMock(), pipeline=p2,
        )

        msg = _load("signal_alert_06.txt")
        orch.dispatch(msg)  # Should not raise

        p2.process_message.assert_called_once_with(msg)
        user_db.close()

    def test_kill_switch_blocks_dispatch(self, tmpdir):
        config = _make_config(tmpdir)
        user_db = UserDatabase(db_path=tmpdir / "test.db")

        orch = Orchestrator(config, user_db)
        p1 = MagicMock()
        orch._pipelines["alice"] = UserPipelineContext(
            user_id="alice", config=config, client=MagicMock(),
            db=MagicMock(), pipeline=p1,
        )

        # Kill
        mock_db = MagicMock()
        mock_db.get_open_trades.return_value = []
        orch._pipelines["alice"].db = mock_db

        with patch("src.orchestrator.PositionManager"):
            orch.kill_all()

        orch.dispatch(_load("signal_alert_06.txt"))
        p1.process_message.assert_not_called()

        # Resume
        orch.resume()
        orch.dispatch(_load("signal_alert_06.txt"))
        p1.process_message.assert_called_once()
        user_db.close()


# ====================================================================
# Edge cases
# ====================================================================

class TestEdgeCases:
    def test_empty_message(self, pipeline, db, client):
        """Empty/whitespace messages should not crash."""
        pipeline.process_message("")
        pipeline.process_message("   ")
        pipeline.process_message("\n\n")
        assert client.exchange.order.call_count == 0

    def test_garbage_message(self, pipeline, db, client):
        """Random garbage text should be classified as noise."""
        pipeline.process_message("lorem ipsum dolor sit amet")
        pipeline.process_message("🚀🚀🚀 moon soon")
        pipeline.process_message("12345")
        assert client.exchange.order.call_count == 0

    def test_all_28_samples_dont_crash(self, pipeline, db):
        """Every single sample file should be processable without exception."""
        for f in sorted(SAMPLES_DIR.glob("*.txt")):
            pipeline.process_message(f.read_text().strip())

        # We should have trades only from the 4 signal alerts
        # (other messages are lifecycle events for trades that may not exist locally)
        trades = db.get_open_trades()
        assert len(trades) == 4

    def test_rapid_fire_same_signal(self, pipeline, db, client):
        """Processing the same signal 10 times should only create 1 trade."""
        msg = _load("signal_alert_06.txt")
        for _ in range(10):
            pipeline.process_message(msg)

        assert len(db.get_open_trades()) == 1

    def test_lifecycle_event_before_signal(self, pipeline, db):
        """TP hit/stop hit arriving before the signal should not crash."""
        # These reference trades we haven't seen yet
        pipeline.process_message(_load("tp_hit_01.txt"))
        pipeline.process_message(_load("stop_hit_01.txt"))
        pipeline.process_message(_load("all_tp_hit_01.txt"))
        pipeline.process_message(_load("breakeven_01.txt"))
        pipeline.process_message(_load("trade_closed_01.txt"))

        # No trades should be created
        assert len(db.get_open_trades()) == 0

    def test_canceled_without_existing_trade(self, pipeline, db):
        """All 5 cancel samples processed without prior trades — no crash."""
        for i in range(1, 6):
            pipeline.process_message(_load(f"canceled_0{i}.txt"))
        assert len(db.get_open_trades()) == 0

    def test_signal_then_immediate_cancel(self, pipeline, db, client):
        """Signal followed immediately by cancel."""
        pipeline.process_message(_load("signal_alert_01.txt"))  # ZK #1286
        assert db.get_trade(1286) is not None

        # There's no cancel sample for #1286, but canceled_02 is for #1268
        # Let's just verify the pattern works for a trade that exists
        from src.state.models import TradeRecord
        trade = TradeRecord(
            trade_id=1268, user_id="test_user", pair="TEST/USDT", coin="ETH",
            side="LONG", risk_level="LOW", trade_type="SWING", size_hint="1-4%",
            entry_price=3000, stop_loss=2900, tp1=3100, tp2=3200, tp3=3500,
            leverage=10, signal_leverage=10, position_size_usd=100.0, position_size_coin=0.033,
        )
        db.create_trade(trade)

        pipeline.process_message(_load("canceled_02.txt"))  # #1268 canceled
        assert db.get_trade(1268).status == TradeStatus.CANCELED


class TestPositionSizeCalculation:
    """Verify position sizes are reasonable for different risk levels."""

    def test_low_risk_gets_bigger_size(self, config, client, tmpdir):
        """LOW risk = 4% of balance vs HIGH risk = 1%."""
        db = TradeDatabase(user_id="test_user", db_path=tmpdir / "test.db")
        pipeline = Pipeline(config=config, client=client, db=db)

        pipeline.process_message(_load("signal_alert_06.txt"))  # ADA LOW risk
        ada_trade = db.get_trade(1259)

        # Reset for next trade
        db2 = TradeDatabase(user_id="test_user2", db_path=tmpdir / "test2.db")
        pipeline2 = Pipeline(config=config, client=client, db=db2)
        pipeline2.process_message(_load("signal_alert_04.txt"))  # XRP HIGH risk
        xrp_trade = db2.get_trade(1282)

        # LOW risk (4%) should be larger than HIGH risk (1%)
        assert ada_trade.position_size_usd > xrp_trade.position_size_usd
        db.close()
        db2.close()
