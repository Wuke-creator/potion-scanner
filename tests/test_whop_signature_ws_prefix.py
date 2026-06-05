"""Tests for Whop webhook signature verification with ws_ hex-encoded secrets.

The Whop developer dashboard provides signing secrets in the format:
    ws_<64 hex chars>   (32 bytes, hex-encoded, with ws_ prefix)

This differs from Resend/Svix which use:
    whsec_<base64>      (base64-encoded key with whsec_ prefix)

Prior to the fix in webhook.py, _svix_signature_ok only handled whsec_
prefixes and bare base64. This caused all Whop event-driven webhooks to
reject with 401 because the hex string was being base64-decoded (garbage
key) instead of hex-decoded.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

import pytest

from src.email_bot.webhook import _svix_signature_ok


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_whop_webhook_headers(
    body: bytes,
    secret_hex: str,
    *,
    msg_id: str = "msg_test123",
    timestamp: int | None = None,
) -> dict[str, str]:
    """Build valid webhook-{id,timestamp,signature} headers (Whop Svix format).

    secret_hex is the raw hex key WITHOUT the ws_ prefix. The caller
    decodes it to get the HMAC key, same as production after the fix.
    """
    ts = int(time.time()) if timestamp is None else int(timestamp)
    secret_bytes = bytes.fromhex(secret_hex)
    signed = f"{msg_id}.{ts}.".encode("utf-8") + body
    sig = base64.b64encode(
        hmac.new(secret_bytes, signed, hashlib.sha256).digest(),
    ).decode("ascii")
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": f"v1,{sig}",
    }


# A realistic 32-byte hex secret like Whop provides.
_TEST_HEX_SECRET = os.urandom(32).hex()
_TEST_WS_SECRET = "ws_" + _TEST_HEX_SECRET

# A fixed known secret for deterministic tests.
_FIXED_HEX = "cc0e1a2b3c4d5e6f7890abcdef123456cc0e1a2b3c4d5e6f7890abcdef123456"
_FIXED_WS = "ws_" + _FIXED_HEX


# ---------------------------------------------------------------------------
# Tests: ws_ prefix (hex-encoded Whop secrets)
# ---------------------------------------------------------------------------


class TestWhopWsHexSignature:
    """Verify _svix_signature_ok works with ws_<hex> format secrets."""

    def test_valid_ws_signature_passes(self):
        """Happy path: properly signed body with ws_ secret verifies."""
        body = b'{"event":"membership.went_valid","data":{"id":"mem_abc"}}'
        headers = _make_whop_webhook_headers(body, _TEST_HEX_SECRET)
        assert _svix_signature_ok(
            body, headers, _TEST_WS_SECRET, header_prefix="webhook",
        ) is True

    def test_fixed_secret_deterministic(self):
        """Same inputs always produce the same result."""
        body = b'{"event":"payment.succeeded"}'
        ts = 1715900000
        headers = _make_whop_webhook_headers(
            body, _FIXED_HEX, timestamp=ts,
        )
        # First call.
        result1 = _svix_signature_ok(
            body, headers, _FIXED_WS, header_prefix="webhook", now=ts,
        )
        # Second call with same inputs.
        result2 = _svix_signature_ok(
            body, headers, _FIXED_WS, header_prefix="webhook", now=ts,
        )
        assert result1 is True
        assert result2 is True

    def test_tampered_body_fails(self):
        """Changing the body after signing must fail verification."""
        body = b'{"event":"membership.went_valid"}'
        headers = _make_whop_webhook_headers(body, _TEST_HEX_SECRET)
        tampered = b'{"event":"membership.went_invalid"}'
        assert _svix_signature_ok(
            tampered, headers, _TEST_WS_SECRET, header_prefix="webhook",
        ) is False

    def test_wrong_secret_fails(self):
        """A different secret must not verify the same signature."""
        body = b'{"event":"payment.failed"}'
        headers = _make_whop_webhook_headers(body, _TEST_HEX_SECRET)
        wrong_secret = "ws_" + os.urandom(32).hex()
        assert _svix_signature_ok(
            body, headers, wrong_secret, header_prefix="webhook",
        ) is False

    def test_expired_timestamp_rejected(self):
        """Timestamps outside the tolerance window are rejected."""
        body = b'{"event":"payment.failed"}'
        old_ts = int(time.time()) - 10 * 60  # 10 minutes ago
        headers = _make_whop_webhook_headers(
            body, _TEST_HEX_SECRET, timestamp=old_ts,
        )
        assert _svix_signature_ok(
            body, headers, _TEST_WS_SECRET, header_prefix="webhook",
        ) is False

    def test_future_timestamp_rejected(self):
        """Far-future timestamps are also rejected."""
        body = b'{"event":"payment.failed"}'
        future_ts = int(time.time()) + 10 * 60
        headers = _make_whop_webhook_headers(
            body, _TEST_HEX_SECRET, timestamp=future_ts,
        )
        assert _svix_signature_ok(
            body, headers, _TEST_WS_SECRET, header_prefix="webhook",
        ) is False

    def test_missing_webhook_id_header_fails(self):
        body = b'{"event":"x"}'
        headers = _make_whop_webhook_headers(body, _TEST_HEX_SECRET)
        del headers["webhook-id"]
        assert _svix_signature_ok(
            body, headers, _TEST_WS_SECRET, header_prefix="webhook",
        ) is False

    def test_missing_webhook_timestamp_header_fails(self):
        body = b'{"event":"x"}'
        headers = _make_whop_webhook_headers(body, _TEST_HEX_SECRET)
        del headers["webhook-timestamp"]
        assert _svix_signature_ok(
            body, headers, _TEST_WS_SECRET, header_prefix="webhook",
        ) is False

    def test_missing_webhook_signature_header_fails(self):
        body = b'{"event":"x"}'
        headers = _make_whop_webhook_headers(body, _TEST_HEX_SECRET)
        del headers["webhook-signature"]
        assert _svix_signature_ok(
            body, headers, _TEST_WS_SECRET, header_prefix="webhook",
        ) is False

    def test_invalid_hex_in_secret_fails_gracefully(self):
        """Malformed hex after ws_ must not crash, just return False."""
        body = b'{"event":"x"}'
        headers = _make_whop_webhook_headers(body, _TEST_HEX_SECRET)
        bad_secret = "ws_zzzz_not_hex_at_all"
        assert _svix_signature_ok(
            body, headers, bad_secret, header_prefix="webhook",
        ) is False

    def test_empty_secret_fails(self):
        body = b'{"event":"x"}'
        headers = _make_whop_webhook_headers(body, _TEST_HEX_SECRET)
        assert _svix_signature_ok(
            body, headers, "", header_prefix="webhook",
        ) is False

    def test_multiple_signatures_one_valid_passes(self):
        """Whop may send multiple v1 entries for key rotation."""
        body = b'{"event":"membership.went_valid"}'
        ts = int(time.time())
        secret_bytes = bytes.fromhex(_TEST_HEX_SECRET)
        signed = f"msg_rot.{ts}.".encode("utf-8") + body
        valid_sig = base64.b64encode(
            hmac.new(secret_bytes, signed, hashlib.sha256).digest(),
        ).decode("ascii")
        garbage_sig = base64.b64encode(b"wrong" * 6).decode("ascii")
        headers = {
            "webhook-id": "msg_rot",
            "webhook-timestamp": str(ts),
            "webhook-signature": f"v1,{garbage_sig} v1,{valid_sig}",
        }
        assert _svix_signature_ok(
            body, headers, _TEST_WS_SECRET, header_prefix="webhook",
        ) is True


# ---------------------------------------------------------------------------
# Tests: confirm whsec_ path still works (no regression)
# ---------------------------------------------------------------------------


class TestWhsecStillWorks:
    """Ensure the fix didn't break the existing whsec_ (Resend) path."""

    _B64_KEY = base64.b64encode(b"resend-key-32-bytes-long-enough!").decode()
    _FULL_SECRET = "whsec_" + _B64_KEY

    def _sign(self, body: bytes, *, ts: int, msg_id: str = "msg_r1"):
        secret_bytes = base64.b64decode(self._B64_KEY)
        signed = f"{msg_id}.{ts}.".encode("utf-8") + body
        sig = base64.b64encode(
            hmac.new(secret_bytes, signed, hashlib.sha256).digest(),
        ).decode("ascii")
        return {
            "svix-id": msg_id,
            "svix-timestamp": str(ts),
            "svix-signature": f"v1,{sig}",
        }

    def test_whsec_valid(self):
        body = b'{"type":"email.delivered"}'
        ts = int(time.time())
        headers = self._sign(body, ts=ts)
        assert _svix_signature_ok(
            body, headers, self._FULL_SECRET, header_prefix="svix", now=ts,
        ) is True

    def test_bare_base64_valid(self):
        """Pasting just the base64 part (no prefix) also works."""
        body = b'{"type":"email.bounced"}'
        ts = int(time.time())
        headers = self._sign(body, ts=ts)
        assert _svix_signature_ok(
            body, headers, self._B64_KEY, header_prefix="svix", now=ts,
        ) is True


# ---------------------------------------------------------------------------
# Integration-style: test the full _whop_signature_ok_svix wrapper
# ---------------------------------------------------------------------------


class TestWhopSignatureOkSvixWrapper:
    """Test the public _whop_signature_ok_svix function end-to-end."""

    def test_via_wrapper(self):
        from src.email_bot.webhook import _whop_signature_ok_svix

        body = b'{"event":"membership.went_valid","data":{"user_id":"user_abc"}}'
        ts = int(time.time())
        secret_bytes = bytes.fromhex(_FIXED_HEX)
        msg_id = "msg_whop_live"
        signed = f"{msg_id}.{ts}.".encode("utf-8") + body
        sig = base64.b64encode(
            hmac.new(secret_bytes, signed, hashlib.sha256).digest(),
        ).decode("ascii")
        headers = {
            "webhook-id": msg_id,
            "webhook-timestamp": str(ts),
            "webhook-signature": f"v1,{sig}",
        }
        assert _whop_signature_ok_svix(
            body, headers, _FIXED_WS, now=ts,
        ) is True
