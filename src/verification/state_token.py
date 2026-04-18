"""HMAC-signed state token for the OAuth round-trip.

Embeds the Telegram user ID + a timestamp + a random nonce into a token
the OAuth callback can validate, ensuring:

  - Cross-Site Request Forgery prevention (only tokens we issued are accepted)
  - The callback knows which Telegram user initiated the verification
  - Replay attacks are bounded by the TTL

Format: ``base64url( payload ) + "." + base64url( hmac_sha256(secret, payload) )``
where ``payload`` is JSON ``{"u": telegram_user_id, "t": iso_timestamp, "n": nonce}``.

No external libraries — uses stdlib hmac/hashlib/secrets/json/base64.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time


class StateTokenError(Exception):
    """Raised when a state token fails validation."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def issue(telegram_user_id: int, secret: str) -> str:
    """Issue a fresh signed state token for a Telegram user."""
    if not secret:
        raise StateTokenError("state token secret is empty")
    payload = {
        "u": int(telegram_user_id),
        "t": int(time.time()),
        "n": secrets.token_hex(8),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).digest()
    return f"{_b64url_encode(payload_bytes)}.{_b64url_encode(sig)}"


def verify(token: str, secret: str, max_age_seconds: int = 600) -> int:
    """Verify a token's signature and expiry. Returns the Telegram user ID.

    Raises StateTokenError on any failure (bad format, bad signature,
    expired, malformed payload).
    """
    if not token or "." not in token:
        raise StateTokenError("malformed state token")

    payload_b64, sig_b64 = token.split(".", 1)
    try:
        payload_bytes = _b64url_decode(payload_b64)
        provided_sig = _b64url_decode(sig_b64)
    except Exception as e:
        raise StateTokenError(f"could not decode state token: {e}") from e

    expected_sig = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_sig, provided_sig):
        raise StateTokenError("state token signature mismatch")

    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError as e:
        raise StateTokenError(f"state token payload is not JSON: {e}") from e

    issued_at = int(payload.get("t", 0))
    user_id = int(payload.get("u", 0))
    if user_id <= 0:
        raise StateTokenError("state token missing telegram user id")

    age = int(time.time()) - issued_at
    if age < 0:
        raise StateTokenError("state token issued in the future")
    if age > max_age_seconds:
        raise StateTokenError(f"state token expired ({age}s old)")

    return user_id
