"""Tests for UserDatabase — CRUD, encryption, config merging."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from src.config.settings import Config
from src.crypto import reset_fernet
from src.state.user_db import UserDatabase


@pytest.fixture(autouse=True)
def _encryption_key():
    """Set a deterministic encryption key for all tests."""
    key = Fernet.generate_key()
    reset_fernet()
    with patch.dict(os.environ, {"ENCRYPTION_KEY": key.decode()}):
        yield
    reset_fernet()


@pytest.fixture
def db():
    """Create a temporary UserDatabase."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        udb = UserDatabase(db_path=db_path)
        yield udb
        udb.close()


SAMPLE_CREDS = {
    "account_address": "0xABC123",
    "api_wallet": "0xWALLET456",
    "api_secret": "0xSECRET789",
    "network": "testnet",
}

SAMPLE_CONFIG = {
    "active_preset": "conservative",
    "auto_execute": True,
    "max_leverage": 10,
    "max_open_positions": 5,
    "max_daily_loss_pct": 5.0,
    "max_position_size_usd": 200.0,
    "max_total_exposure_usd": 1000.0,
    "min_order_usd": 15.0,
}


class TestCreateUser:
    def test_create_user_basic(self, db):
        user = db.create_user("alice", "Alice", SAMPLE_CREDS)
        assert user.user_id == "alice"
        assert user.display_name == "Alice"
        assert user.status == "active"

    def test_create_user_with_config(self, db):
        db.create_user("bob", "Bob", SAMPLE_CREDS, config=SAMPLE_CONFIG)
        cfg = db.get_user_config("bob")
        assert cfg["active_preset"] == "conservative"
        assert cfg["auto_execute"] is True
        assert cfg["max_leverage"] == 10

    def test_create_duplicate_raises(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        with pytest.raises(Exception):
            db.create_user("alice", "Alice2", SAMPLE_CREDS)


class TestGetUser:
    def test_get_existing_user(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        user = db.get_user("alice")
        assert user is not None
        assert user.display_name == "Alice"

    def test_get_nonexistent_returns_none(self, db):
        assert db.get_user("nobody") is None


class TestListUsers:
    def test_list_all(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.create_user("bob", "Bob", SAMPLE_CREDS)
        users = db.list_users()
        assert len(users) == 2

    def test_list_by_status(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.create_user("bob", "Bob", SAMPLE_CREDS)
        db.set_user_status("bob", "inactive")

        active = db.list_users(status="active")
        assert len(active) == 1
        assert active[0].user_id == "alice"

        inactive = db.list_users(status="inactive")
        assert len(inactive) == 1
        assert inactive[0].user_id == "bob"

    def test_get_active_users(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.create_user("bob", "Bob", SAMPLE_CREDS)
        db.set_user_status("bob", "inactive")
        active = db.get_active_users()
        assert len(active) == 1
        assert active[0].user_id == "alice"


class TestSetUserStatus:
    def test_deactivate_and_reactivate(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.set_user_status("alice", "inactive")
        assert db.get_user("alice").status == "inactive"
        db.set_user_status("alice", "active")
        assert db.get_user("alice").status == "active"


class TestCredentials:
    def test_credentials_encrypted_at_rest(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        # Read raw from DB — should NOT be plaintext
        row = db._conn.execute(
            "SELECT account_address_enc FROM user_credentials WHERE user_id = 'alice'"
        ).fetchone()
        assert row["account_address_enc"] != "0xABC123"

    def test_credentials_decrypted_correctly(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        creds = db.get_user_credentials_decrypted("alice")
        assert creds["account_address"] == "0xABC123"
        assert creds["api_wallet"] == "0xWALLET456"
        assert creds["api_secret"] == "0xSECRET789"
        assert creds["network"] == "testnet"

    def test_nonexistent_credentials_returns_none(self, db):
        assert db.get_user_credentials_decrypted("nobody") is None

    def test_update_credentials(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.update_user_credentials("alice", account_address="0xNEW", network="mainnet")
        creds = db.get_user_credentials_decrypted("alice")
        assert creds["account_address"] == "0xNEW"
        assert creds["network"] == "mainnet"
        # Unchanged fields stay the same
        assert creds["api_secret"] == "0xSECRET789"


class TestUserConfig:
    def test_default_config(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        cfg = db.get_user_config("alice")
        assert cfg["active_preset"] == "runner"
        assert cfg["auto_execute"] is False
        assert cfg["max_leverage"] == 20
        assert cfg["max_open_positions"] == 10

    def test_custom_config(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS, config=SAMPLE_CONFIG)
        cfg = db.get_user_config("alice")
        assert cfg["active_preset"] == "conservative"
        assert cfg["auto_execute"] is True
        assert cfg["max_leverage"] == 10
        assert cfg["max_position_size_usd"] == 200.0

    def test_update_config(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.update_user_config("alice", active_preset="tp2_exit", max_leverage=15)
        cfg = db.get_user_config("alice")
        assert cfg["active_preset"] == "tp2_exit"
        assert cfg["max_leverage"] == 15

    def test_update_json_config(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        new_risk = {"LOW": 5.0, "MEDIUM": 3.0, "HIGH": 1.5}
        db.update_user_config("alice", size_by_risk=new_risk)
        cfg = db.get_user_config("alice")
        assert cfg["size_by_risk"] == new_risk

    def test_nonexistent_config_returns_none(self, db):
        assert db.get_user_config("nobody") is None


class TestGetUserConfigAsConfig:
    def test_builds_valid_config(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS, config=SAMPLE_CONFIG)
        global_config = Config()
        cfg = db.get_user_config_as_config("alice", global_config)

        # Exchange should come from user credentials
        assert cfg.exchange.account_address == "0xABC123"
        assert cfg.exchange.api_secret == "0xSECRET789"
        assert cfg.exchange.network == "testnet"

        # Strategy from user config
        assert cfg.strategy.active_preset == "conservative"
        assert cfg.strategy.auto_execute is True
        assert cfg.strategy.max_leverage == 10

        # Risk from user config
        assert cfg.risk.max_open_positions == 5
        assert cfg.risk.max_daily_loss_pct == 5.0
        assert cfg.risk.max_position_size_usd == 200.0

        # Input/database/logging from global config
        assert cfg.input == global_config.input
        assert cfg.database == global_config.database

    def test_nonexistent_user_raises(self, db):
        global_config = Config()
        with pytest.raises(ValueError, match="not found"):
            db.get_user_config_as_config("nobody", global_config)

    def test_custom_presets_included(self, db):
        config_with_preset = {
            **SAMPLE_CONFIG,
            "custom_presets": {
                "my_preset": {
                    "tp_split": [0.5, 0.3, 0.2],
                    "move_sl_to_breakeven_after": "tp2",
                    "size_pct": 3.0,
                }
            },
        }
        db.create_user("alice", "Alice", SAMPLE_CREDS, config=config_with_preset)
        cfg = db.get_user_config_as_config("alice", Config())
        assert "my_preset" in cfg.strategy.presets
        assert cfg.strategy.presets["my_preset"].tp_split == [0.5, 0.3, 0.2]
        assert cfg.strategy.presets["my_preset"].size_pct == 3.0
