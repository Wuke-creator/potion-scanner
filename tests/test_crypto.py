"""Tests for Fernet encryption utilities."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet, InvalidToken

from src.crypto import decrypt, encrypt, get_fernet, reset_fernet


@pytest.fixture(autouse=True)
def _clean_fernet():
    """Reset cached Fernet instance before and after each test."""
    reset_fernet()
    yield
    reset_fernet()


class TestEncryptDecrypt:
    """Round-trip encryption/decryption tests."""

    def test_round_trip(self):
        key = Fernet.generate_key()
        with patch.dict(os.environ, {"ENCRYPTION_KEY": key.decode()}):
            plaintext = "my-secret-api-key-0xabc123"
            ciphertext = encrypt(plaintext)
            assert ciphertext != plaintext
            assert decrypt(ciphertext) == plaintext

    def test_different_plaintexts_produce_different_ciphertexts(self):
        key = Fernet.generate_key()
        with patch.dict(os.environ, {"ENCRYPTION_KEY": key.decode()}):
            c1 = encrypt("secret1")
            c2 = encrypt("secret2")
            assert c1 != c2

    def test_empty_string_round_trip(self):
        key = Fernet.generate_key()
        with patch.dict(os.environ, {"ENCRYPTION_KEY": key.decode()}):
            assert decrypt(encrypt("")) == ""

    def test_unicode_round_trip(self):
        key = Fernet.generate_key()
        with patch.dict(os.environ, {"ENCRYPTION_KEY": key.decode()}):
            text = "Hllo Wrld! \u2603"
            assert decrypt(encrypt(text)) == text


class TestWrongKey:
    """Decryption with wrong key should fail."""

    def test_wrong_key_raises(self):
        key1 = Fernet.generate_key()
        key2 = Fernet.generate_key()

        with patch.dict(os.environ, {"ENCRYPTION_KEY": key1.decode()}):
            ciphertext = encrypt("secret")

        reset_fernet()

        with patch.dict(os.environ, {"ENCRYPTION_KEY": key2.decode()}):
            with pytest.raises(InvalidToken):
                decrypt(ciphertext)


class TestKeyFromEnvVar:
    """Key loading from ENCRYPTION_KEY env var."""

    def test_uses_env_var(self):
        key = Fernet.generate_key()
        with patch.dict(os.environ, {"ENCRYPTION_KEY": key.decode()}):
            f = get_fernet()
            assert f is not None
            # Verify it works
            assert decrypt(encrypt("test")) == "test"


class TestAutoGenerateKeyFile:
    """Auto-generation of key file when env var is absent."""

    def test_auto_generates_key_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / ".encryption_key"
            with patch.dict(os.environ, {}, clear=False):
                # Remove ENCRYPTION_KEY if present
                os.environ.pop("ENCRYPTION_KEY", None)
                with patch("src.crypto._KEY_FILE", key_file):
                    reset_fernet()
                    f = get_fernet()
                    assert f is not None
                    assert key_file.exists()
                    # Key file should contain valid Fernet key
                    stored_key = key_file.read_bytes().strip()
                    Fernet(stored_key)  # Should not raise

    def test_reuses_existing_key_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = Path(tmpdir) / ".encryption_key"
            key = Fernet.generate_key()
            key_file.write_bytes(key)

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ENCRYPTION_KEY", None)
                with patch("src.crypto._KEY_FILE", key_file):
                    reset_fernet()
                    ciphertext = encrypt("hello")
                    reset_fernet()
                    assert decrypt(ciphertext) == "hello"
