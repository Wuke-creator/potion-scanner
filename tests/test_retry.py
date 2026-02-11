"""Tests for the retry_on_transient decorator."""

import pytest

from src.exchange.hyperliquid import retry_on_transient


class TestRetryOnTransient:
    """Verify retry behavior for transient vs non-transient errors."""

    def test_success_no_retry(self):
        call_count = 0

        @retry_on_transient(max_retries=3, base_delay=0.01)
        def succeed():
            nonlocal call_count
            call_count += 1
            return "ok"

        assert succeed() == "ok"
        assert call_count == 1

    def test_transient_error_retries_then_succeeds(self):
        call_count = 0

        @retry_on_transient(max_retries=3, base_delay=0.01)
        def fail_twice():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("connection reset")
            return "recovered"

        assert fail_twice() == "recovered"
        assert call_count == 3

    def test_max_retries_exceeded_raises(self):
        call_count = 0

        @retry_on_transient(max_retries=2, base_delay=0.01)
        def always_fail():
            nonlocal call_count
            call_count += 1
            raise TimeoutError("timed out")

        with pytest.raises(TimeoutError, match="timed out"):
            always_fail()
        assert call_count == 3  # initial + 2 retries

    def test_non_transient_error_raises_immediately(self):
        call_count = 0

        @retry_on_transient(max_retries=3, base_delay=0.01)
        def bad_value():
            nonlocal call_count
            call_count += 1
            raise ValueError("invalid argument")

        with pytest.raises(ValueError, match="invalid argument"):
            bad_value()
        assert call_count == 1  # no retry

    def test_rate_limit_string_triggers_retry(self):
        call_count = 0

        @retry_on_transient(max_retries=2, base_delay=0.01)
        def rate_limited():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise Exception("Rate limit exceeded (429)")
            return "ok"

        assert rate_limited() == "ok"
        assert call_count == 2

    def test_os_error_retries(self):
        call_count = 0

        @retry_on_transient(max_retries=2, base_delay=0.01)
        def network_blip():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise OSError("network unreachable")
            return "ok"

        assert network_blip() == "ok"
        assert call_count == 2
