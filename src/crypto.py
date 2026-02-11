"""Fernet symmetric encryption for credential storage.

Master key sourced from ENCRYPTION_KEY env var. If unset, auto-generates
and persists to data/.encryption_key for single-server deployments.
"""

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

_KEY_FILE = Path("data/.encryption_key")


def _load_or_generate_key() -> bytes:
    """Load the encryption key from env var or key file, generating if needed."""
    env_key = os.getenv("ENCRYPTION_KEY")
    if env_key:
        logger.info("Using encryption key from ENCRYPTION_KEY env var")
        return env_key.encode()

    if _KEY_FILE.exists():
        logger.info("Using encryption key from %s", _KEY_FILE)
        return _KEY_FILE.read_bytes().strip()

    # Auto-generate and save
    key = Fernet.generate_key()
    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _KEY_FILE.write_bytes(key)
    logger.warning("Generated new encryption key → %s", _KEY_FILE)
    return key


_fernet: Fernet | None = None


def get_fernet() -> Fernet:
    """Return a Fernet instance, initializing on first call."""
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_generate_key())
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string, returning base64-encoded ciphertext."""
    return get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a base64-encoded ciphertext, returning the original plaintext."""
    return get_fernet().decrypt(ciphertext.encode()).decode()


def reset_fernet() -> None:
    """Reset the cached Fernet instance (useful for testing)."""
    global _fernet
    _fernet = None
