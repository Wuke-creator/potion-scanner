"""Tests for src/verification/state_token.py — HMAC sign + verify roundtrip."""

import time

import pytest

from src.verification.state_token import StateTokenError, issue, verify


SECRET = "test-secret-32-bytes-of-entropy-here"
OTHER_SECRET = "different-secret-also-32-bytes-of-stuff"


class TestRoundtrip:
    def test_issue_then_verify_returns_user_id(self):
        token = issue(12345, SECRET)
        assert verify(token, SECRET) == 12345

    def test_each_token_is_unique_due_to_nonce(self):
        a = issue(12345, SECRET)
        b = issue(12345, SECRET)
        # Same user, same secret, but different nonces → different tokens
        assert a != b
        assert verify(a, SECRET) == 12345
        assert verify(b, SECRET) == 12345

    def test_different_users_get_different_tokens(self):
        a = issue(111, SECRET)
        b = issue(222, SECRET)
        assert verify(a, SECRET) == 111
        assert verify(b, SECRET) == 222


class TestVerifyFailures:
    def test_empty_token_rejected(self):
        with pytest.raises(StateTokenError):
            verify("", SECRET)

    def test_malformed_token_rejected(self):
        with pytest.raises(StateTokenError):
            verify("not-a-token", SECRET)

    def test_signature_with_different_secret_rejected(self):
        token = issue(12345, SECRET)
        with pytest.raises(StateTokenError, match="signature"):
            verify(token, OTHER_SECRET)

    def test_tampered_payload_rejected(self):
        token = issue(12345, SECRET)
        payload, sig = token.split(".", 1)
        # Flip a byte in the payload
        tampered = payload[:-2] + ("AA" if payload[-2:] != "AA" else "BB") + "." + sig
        with pytest.raises(StateTokenError):
            verify(tampered, SECRET)

    def test_expired_token_rejected(self, monkeypatch):
        token = issue(12345, SECRET)
        # Pretend 700 seconds passed (default max_age=600)
        original_time = time.time
        monkeypatch.setattr(time, "time", lambda: original_time() + 700)
        with pytest.raises(StateTokenError, match="expired"):
            verify(token, SECRET)

    def test_short_max_age_rejects_aged_token(self, monkeypatch):
        token = issue(12345, SECRET)
        original_time = time.time
        monkeypatch.setattr(time, "time", lambda: original_time() + 30)
        with pytest.raises(StateTokenError, match="expired"):
            verify(token, SECRET, max_age_seconds=10)

    def test_empty_secret_rejected_at_issue(self):
        with pytest.raises(StateTokenError):
            issue(12345, "")
