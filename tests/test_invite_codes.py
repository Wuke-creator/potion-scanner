"""Tests for invite code system — generation, validation, redemption, expiry."""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from src.crypto import reset_fernet
from src.state.user_db import UserDatabase
from src.telegram.invite_codes import generate_invite_code


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


class TestGenerateInviteCode:
    def test_format(self):
        code = generate_invite_code()
        parts = code.split("-")
        assert len(parts) == 3
        assert parts[0] == "PPB"
        assert len(parts[1]) == 4
        assert len(parts[2]) == 4

    def test_uniqueness(self):
        codes = {generate_invite_code() for _ in range(100)}
        assert len(codes) == 100

    def test_no_ambiguous_chars(self):
        for _ in range(50):
            code = generate_invite_code()
            # Remove the PPB- prefix and dashes
            chars = code.replace("PPB-", "").replace("-", "")
            for c in chars:
                assert c not in "0OIL1"


class TestCreateInviteCode:
    def test_create_unlimited(self, db):
        result = db.create_invite_code("PPB-TEST-CODE", "admin1")
        assert result["code"] == "PPB-TEST-CODE"
        assert result["created_by"] == "admin1"
        assert result["duration_days"] is None
        assert result["status"] == "active"

    def test_create_with_duration(self, db):
        result = db.create_invite_code("PPB-TEST-CODE", "admin1", duration_days=30)
        assert result["duration_days"] == 30

    def test_duplicate_code_raises(self, db):
        db.create_invite_code("PPB-TEST-CODE", "admin1")
        with pytest.raises(Exception):
            db.create_invite_code("PPB-TEST-CODE", "admin1")


class TestValidateInviteCode:
    def test_valid_code(self, db):
        db.create_invite_code("PPB-TEST-CODE", "admin1")
        result = db.validate_invite_code("PPB-TEST-CODE")
        assert result["valid"] is True
        assert result["code"] == "PPB-TEST-CODE"

    def test_nonexistent_code(self, db):
        result = db.validate_invite_code("PPB-NOPE-NOPE")
        assert result["valid"] is False
        assert "not found" in result["reason"].lower()

    def test_redeemed_code(self, db):
        db.create_invite_code("PPB-TEST-CODE", "admin1")
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.redeem_invite_code("PPB-TEST-CODE", "alice")

        result = db.validate_invite_code("PPB-TEST-CODE")
        assert result["valid"] is False
        assert "redeemed" in result["reason"].lower()

    def test_revoked_code(self, db):
        db.create_invite_code("PPB-TEST-CODE", "admin1")
        db.revoke_invite_code("PPB-TEST-CODE")

        result = db.validate_invite_code("PPB-TEST-CODE")
        assert result["valid"] is False
        assert "revoked" in result["reason"].lower()


class TestRedeemInviteCode:
    def test_redeem_unlimited(self, db):
        db.create_invite_code("PPB-TEST-CODE", "admin1")
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        expires = db.redeem_invite_code("PPB-TEST-CODE", "alice")

        assert expires is None  # unlimited

        # Code should be marked redeemed
        codes = db.list_invite_codes()
        assert codes[0]["status"] == "redeemed"
        assert codes[0]["redeemed_by"] == "alice"

    def test_redeem_with_duration(self, db):
        db.create_invite_code("PPB-TEST-CODE", "admin1", duration_days=30)
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        expires = db.redeem_invite_code("PPB-TEST-CODE", "alice")

        assert expires is not None
        expiry_dt = datetime.fromisoformat(expires)
        # Should be roughly 30 days from now
        expected = datetime.now(timezone.utc) + timedelta(days=30)
        assert abs((expiry_dt - expected).total_seconds()) < 5

    def test_redeem_sets_user_config(self, db):
        db.create_invite_code("PPB-TEST-CODE", "admin1", duration_days=30)
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.redeem_invite_code("PPB-TEST-CODE", "alice")

        expiry = db.get_access_expiry("alice")
        assert expiry is not None

    def test_redeem_nonexistent_raises(self, db):
        with pytest.raises(ValueError, match="not found"):
            db.redeem_invite_code("PPB-NOPE-NOPE", "alice")


class TestListInviteCodes:
    def test_list_all(self, db):
        db.create_invite_code("PPB-AAA1-BBB1", "admin1")
        db.create_invite_code("PPB-AAA2-BBB2", "admin1", duration_days=30)
        codes = db.list_invite_codes()
        assert len(codes) == 2

    def test_list_by_status(self, db):
        db.create_invite_code("PPB-AAA1-BBB1", "admin1")
        db.create_invite_code("PPB-AAA2-BBB2", "admin1")
        db.revoke_invite_code("PPB-AAA2-BBB2")

        active = db.list_invite_codes(status="active")
        assert len(active) == 1
        assert active[0]["code"] == "PPB-AAA1-BBB1"

        revoked = db.list_invite_codes(status="revoked")
        assert len(revoked) == 1
        assert revoked[0]["code"] == "PPB-AAA2-BBB2"


class TestRevokeInviteCode:
    def test_revoke_active(self, db):
        db.create_invite_code("PPB-TEST-CODE", "admin1")
        assert db.revoke_invite_code("PPB-TEST-CODE") is True

        result = db.validate_invite_code("PPB-TEST-CODE")
        assert result["valid"] is False

    def test_revoke_nonexistent(self, db):
        assert db.revoke_invite_code("PPB-NOPE-NOPE") is False

    def test_revoke_already_redeemed(self, db):
        db.create_invite_code("PPB-TEST-CODE", "admin1")
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.redeem_invite_code("PPB-TEST-CODE", "alice")

        assert db.revoke_invite_code("PPB-TEST-CODE") is False


class TestTelegramChatId:
    def test_set_and_get(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.set_telegram_chat_id("alice", 123456789)
        assert db.get_telegram_chat_id("alice") == 123456789

    def test_get_nonexistent(self, db):
        assert db.get_telegram_chat_id("nobody") is None

    def test_get_unset(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        assert db.get_telegram_chat_id("alice") is None

    def test_lookup_by_chat_id(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.set_telegram_chat_id("alice", 123456789)
        assert db.get_user_by_telegram_chat_id(123456789) == "alice"

    def test_lookup_by_chat_id_not_found(self, db):
        assert db.get_user_by_telegram_chat_id(999999) is None

    def test_get_all_chat_ids(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.create_user("bob", "Bob", SAMPLE_CREDS)
        db.set_telegram_chat_id("alice", 111)
        db.set_telegram_chat_id("bob", 222)

        ids = db.get_all_telegram_chat_ids()
        assert set(ids) == {111, 222}

    def test_get_all_chat_ids_excludes_inactive(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.create_user("bob", "Bob", SAMPLE_CREDS)
        db.set_telegram_chat_id("alice", 111)
        db.set_telegram_chat_id("bob", 222)
        db.set_user_status("bob", "inactive")

        ids = db.get_all_telegram_chat_ids()
        assert ids == [111]


class TestAccessExpiry:
    def test_get_expiry_unlimited(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        assert db.get_access_expiry("alice") is None

    def test_get_expired_users(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.create_user("bob", "Bob", SAMPLE_CREDS)

        # Set alice as expired (past date)
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        db._conn.execute(
            "UPDATE user_config SET access_expires_at = ? WHERE user_id = ?",
            (past, "alice"),
        )
        db._conn.commit()

        # Set bob as not expired (future date)
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        db._conn.execute(
            "UPDATE user_config SET access_expires_at = ? WHERE user_id = ?",
            (future, "bob"),
        )
        db._conn.commit()

        expired = db.get_expired_users()
        assert expired == ["alice"]

    def test_extend_access(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        new_expiry = db.extend_user_access("alice", 30)

        expiry_dt = datetime.fromisoformat(new_expiry)
        expected = datetime.now(timezone.utc) + timedelta(days=30)
        assert abs((expiry_dt - expected).total_seconds()) < 5

        assert db.get_access_expiry("alice") == new_expiry

    def test_revoke_access(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.revoke_user_access("alice")

        user = db.get_user("alice")
        assert user.status == "inactive"

        expiry = db.get_access_expiry("alice")
        assert expiry is not None
        expiry_dt = datetime.fromisoformat(expiry)
        assert expiry_dt <= datetime.now(timezone.utc)
