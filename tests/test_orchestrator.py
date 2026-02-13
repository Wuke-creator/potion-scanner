"""Tests for the multi-user Orchestrator."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from src.config.settings import Config, ExchangeConfig
from src.crypto import reset_fernet
from src.health import HealthServer
from src.orchestrator import Orchestrator, UserPipelineContext
from src.state.database import TradeDatabase
from src.state.models import TradeRecord, TradeStatus
from src.state.user_db import UserDatabase

SAMPLE_CREDS = {
    "account_address": "0xABC123",
    "api_wallet": "0xWALLET456",
    "api_secret": "0xSECRET789",
    "network": "testnet",
}


@pytest.fixture(autouse=True)
def _encryption_key():
    key = Fernet.generate_key()
    reset_fernet()
    with patch.dict(os.environ, {"ENCRYPTION_KEY": key.decode()}):
        yield
    reset_fernet()


@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def user_db(tmpdir):
    db_path = tmpdir / "test.db"
    udb = UserDatabase(db_path=db_path)
    yield udb
    udb.close()


@pytest.fixture
def global_config(tmpdir):
    return Config(
        exchange=ExchangeConfig(network="testnet"),
        database=MagicMock(path=str(tmpdir / "test.db")),
    )


def _mock_client():
    """Create a mock HyperliquidClient."""
    client = MagicMock()
    client.get_balance.return_value = {"usdc_balance": "1000"}
    client.get_open_positions.return_value = []
    client.get_open_orders.return_value = []
    client.get_asset_meta.return_value = {}
    return client


class TestDispatch:
    def test_dispatch_fans_out_to_all_pipelines(self, global_config, user_db):
        orch = Orchestrator(global_config, user_db)

        # Manually add mock pipelines
        p1 = MagicMock()
        p2 = MagicMock()
        orch._pipelines["user1"] = UserPipelineContext(
            user_id="user1", config=global_config, client=MagicMock(),
            db=MagicMock(), pipeline=p1,
        )
        orch._pipelines["user2"] = UserPipelineContext(
            user_id="user2", config=global_config, client=MagicMock(),
            db=MagicMock(), pipeline=p2,
        )

        orch.dispatch("test signal message")

        p1.process_message.assert_called_once_with("test signal message")
        p2.process_message.assert_called_once_with("test signal message")

    def test_dispatch_records_health(self, global_config, user_db):
        orch = Orchestrator(global_config, user_db)
        orch._pipelines["user1"] = UserPipelineContext(
            user_id="user1", config=global_config, client=MagicMock(),
            db=MagicMock(), pipeline=MagicMock(),
        )
        health = MagicMock()
        orch.dispatch("msg", health_server=health)
        health.record_message.assert_called_once()

    def test_dispatch_no_pipelines_warns(self, global_config, user_db):
        orch = Orchestrator(global_config, user_db)
        # Should not raise
        orch.dispatch("msg")


class TestErrorIsolation:
    def test_one_pipeline_error_doesnt_crash_others(self, global_config, user_db):
        orch = Orchestrator(global_config, user_db)

        p1 = MagicMock()
        p1.process_message.side_effect = RuntimeError("boom")
        p2 = MagicMock()

        orch._pipelines["user1"] = UserPipelineContext(
            user_id="user1", config=global_config, client=MagicMock(),
            db=MagicMock(), pipeline=p1,
        )
        orch._pipelines["user2"] = UserPipelineContext(
            user_id="user2", config=global_config, client=MagicMock(),
            db=MagicMock(), pipeline=p2,
        )

        # Should not raise despite user1's error
        orch.dispatch("test signal")

        p1.process_message.assert_called_once()
        p2.process_message.assert_called_once()


class TestActivateDeactivate:
    @patch("src.orchestrator.HyperliquidClient")
    @patch("src.orchestrator.PositionManager")
    @patch("src.orchestrator.Pipeline")
    def test_activate_user(self, MockPipeline, MockPM, MockClient, global_config, user_db):
        MockClient.return_value = _mock_client()
        MockPM.return_value.sync_positions.return_value = {
            "closed": [], "canceled": [], "verified": [], "orphans": [],
        }

        user_db.create_user("alice", "Alice", SAMPLE_CREDS)
        orch = Orchestrator(global_config, user_db)
        orch.activate_user("alice")

        assert "alice" in orch.pipelines
        MockClient.assert_called_once()
        MockPipeline.assert_called_once()

    @patch("src.orchestrator.HyperliquidClient")
    @patch("src.orchestrator.PositionManager")
    @patch("src.orchestrator.Pipeline")
    def test_deactivate_user(self, MockPipeline, MockPM, MockClient, global_config, user_db):
        MockClient.return_value = _mock_client()
        MockPM.return_value.sync_positions.return_value = {
            "closed": [], "canceled": [], "verified": [], "orphans": [],
        }

        user_db.create_user("alice", "Alice", SAMPLE_CREDS)
        orch = Orchestrator(global_config, user_db)
        orch.activate_user("alice")
        assert "alice" in orch.pipelines

        orch.deactivate_user("alice")
        assert "alice" not in orch.pipelines

    def test_deactivate_nonexistent_user(self, global_config, user_db):
        orch = Orchestrator(global_config, user_db)
        # Should not raise
        orch.deactivate_user("nobody")

    @patch("src.orchestrator.HyperliquidClient")
    @patch("src.orchestrator.PositionManager")
    @patch("src.orchestrator.Pipeline")
    def test_activate_already_active_skips(self, MockPipeline, MockPM, MockClient, global_config, user_db):
        MockClient.return_value = _mock_client()
        MockPM.return_value.sync_positions.return_value = {
            "closed": [], "canceled": [], "verified": [], "orphans": [],
        }

        user_db.create_user("alice", "Alice", SAMPLE_CREDS)
        orch = Orchestrator(global_config, user_db)
        orch.activate_user("alice")
        orch.activate_user("alice")  # Should skip silently

        assert len(orch.pipelines) == 1


class TestEnvFallback:
    @patch("src.orchestrator.HyperliquidClient")
    @patch("src.orchestrator.PositionManager")
    @patch("src.orchestrator.Pipeline")
    def test_fallback_to_single_user(self, MockPipeline, MockPM, MockClient, tmpdir):
        MockClient.return_value = _mock_client()
        MockPM.return_value.sync_positions.return_value = {
            "closed": [], "canceled": [], "verified": [], "orphans": [],
        }

        config = Config(
            exchange=ExchangeConfig(
                network="testnet",
                account_address="0xENV_ADDR",
                api_secret="0xENV_SECRET",
            ),
            database=MagicMock(path=str(tmpdir / "test.db")),
        )
        user_db = UserDatabase(db_path=tmpdir / "test.db")

        with patch.dict(os.environ, {"HL_ACCOUNT_ADDRESS": "0xENV_ADDR"}):
            orch = Orchestrator(config, user_db)
            orch.start()

        assert "default" in orch.pipelines
        orch.stop()
        user_db.close()

    def test_no_users_no_env_starts_empty(self, tmpdir):
        config = Config(
            exchange=ExchangeConfig(network="testnet"),
            database=MagicMock(path=str(tmpdir / "test.db")),
        )
        user_db = UserDatabase(db_path=tmpdir / "test.db")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HL_ACCOUNT_ADDRESS", None)
            orch = Orchestrator(config, user_db)
            orch.start()

        assert len(orch.pipelines) == 0
        orch.stop()
        user_db.close()


class TestKillSwitch:
    def test_kill_blocks_dispatch(self, global_config, user_db):
        orch = Orchestrator(global_config, user_db)
        pipeline = MagicMock()
        orch._pipelines["u1"] = UserPipelineContext(
            user_id="u1", config=global_config, client=MagicMock(),
            db=MagicMock(), pipeline=pipeline,
        )

        orch._killed = True
        orch.dispatch("test signal")

        pipeline.process_message.assert_not_called()

    def test_kill_all_sets_flag(self, global_config, user_db):
        orch = Orchestrator(global_config, user_db)
        mock_db = MagicMock()
        mock_db.get_open_trades.return_value = []
        orch._pipelines["u1"] = UserPipelineContext(
            user_id="u1", config=global_config, client=MagicMock(),
            db=mock_db, pipeline=MagicMock(),
        )

        assert not orch.killed
        orch.kill_all()
        assert orch.killed

    def test_kill_all_closes_positions(self, global_config, user_db):
        orch = Orchestrator(global_config, user_db)

        mock_trade = MagicMock()
        mock_trade.trade_id = 1
        mock_trade.coin = "BTC"

        mock_db = MagicMock()
        mock_db.get_open_trades.return_value = [mock_trade]

        mock_client = MagicMock()

        orch._pipelines["u1"] = UserPipelineContext(
            user_id="u1", config=global_config, client=mock_client,
            db=mock_db, pipeline=MagicMock(),
        )

        with patch("src.orchestrator.PositionManager") as MockPM:
            results = orch.kill_all()

        assert results["u1"]["closed"] == 1
        assert orch.killed

    def test_kill_all_error_isolation(self, global_config, user_db):
        """One user's close failure shouldn't prevent other users from being processed."""
        orch = Orchestrator(global_config, user_db)

        mock_trade = MagicMock()
        mock_trade.trade_id = 1
        mock_trade.coin = "BTC"

        # User 1 — will fail
        mock_db1 = MagicMock()
        mock_db1.get_open_trades.return_value = [mock_trade]
        # User 2 — no open trades
        mock_db2 = MagicMock()
        mock_db2.get_open_trades.return_value = []

        orch._pipelines["u1"] = UserPipelineContext(
            user_id="u1", config=global_config, client=MagicMock(),
            db=mock_db1, pipeline=MagicMock(),
        )
        orch._pipelines["u2"] = UserPipelineContext(
            user_id="u2", config=global_config, client=MagicMock(),
            db=mock_db2, pipeline=MagicMock(),
        )

        with patch("src.orchestrator.PositionManager") as MockPM:
            pm_instance = MockPM.return_value
            pm_instance.close_position.side_effect = RuntimeError("exchange down")
            results = orch.kill_all()

        assert len(results["u1"]["errors"]) == 1
        assert "u2" in results

    def test_resume_clears_flag(self, global_config, user_db):
        orch = Orchestrator(global_config, user_db)
        orch._killed = True
        orch.resume()
        assert not orch.killed

    def test_resume_allows_dispatch(self, global_config, user_db):
        orch = Orchestrator(global_config, user_db)
        pipeline = MagicMock()
        orch._pipelines["u1"] = UserPipelineContext(
            user_id="u1", config=global_config, client=MagicMock(),
            db=MagicMock(), pipeline=pipeline,
        )

        orch._killed = True
        orch.dispatch("blocked")
        pipeline.process_message.assert_not_called()

        orch.resume()
        orch.dispatch("allowed")
        pipeline.process_message.assert_called_once_with("allowed")


class TestStop:
    def test_stop_closes_all(self, global_config, user_db):
        orch = Orchestrator(global_config, user_db)
        db1 = MagicMock()
        db2 = MagicMock()
        orch._pipelines["u1"] = UserPipelineContext(
            user_id="u1", config=global_config, client=MagicMock(),
            db=db1, pipeline=MagicMock(),
        )
        orch._pipelines["u2"] = UserPipelineContext(
            user_id="u2", config=global_config, client=MagicMock(),
            db=db2, pipeline=MagicMock(),
        )

        orch.stop()

        assert len(orch.pipelines) == 0
        db1.close.assert_called_once()
        db2.close.assert_called_once()
