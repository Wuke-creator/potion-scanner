"""Tests for src/rate_limiter.py — AsyncTokenBucket."""

import asyncio
import time

import pytest

from src.rate_limiter import AsyncTokenBucket


@pytest.mark.asyncio
class TestAsyncTokenBucket:
    async def test_initial_burst_is_instant(self):
        bucket = AsyncTokenBucket(rate_per_sec=10, capacity=10)
        start = time.monotonic()
        for _ in range(10):
            await bucket.take(1)
        elapsed = time.monotonic() - start
        # 10 tokens from a full bucket should be effectively instant
        assert elapsed < 0.05

    async def test_rate_limit_throttles_after_burst(self):
        bucket = AsyncTokenBucket(rate_per_sec=20, capacity=5)
        start = time.monotonic()
        for _ in range(10):  # 5 free, then 5 at 20/sec = 250ms total
            await bucket.take(1)
        elapsed = time.monotonic() - start
        # 5 tokens in the burst + 5 more at 20/sec → ~0.25s, allow slack
        assert 0.15 < elapsed < 0.5

    async def test_concurrent_takers_share_the_bucket(self):
        bucket = AsyncTokenBucket(rate_per_sec=50, capacity=10)

        async def taker():
            await bucket.take(1)

        start = time.monotonic()
        # 50 concurrent takers from a 10-token bucket at 50/sec
        # = 10 instant + 40 over ~0.8s
        await asyncio.gather(*(taker() for _ in range(50)))
        elapsed = time.monotonic() - start
        assert 0.4 < elapsed < 1.5

    async def test_take_zero_is_noop(self):
        bucket = AsyncTokenBucket(rate_per_sec=10, capacity=5)
        await bucket.take(0)
        assert bucket.available == pytest.approx(5.0, abs=0.1)

    async def test_take_more_than_capacity_raises(self):
        bucket = AsyncTokenBucket(rate_per_sec=10, capacity=5)
        with pytest.raises(ValueError, match="capacity"):
            await bucket.take(10)

    async def test_invalid_rate_raises(self):
        with pytest.raises(ValueError):
            AsyncTokenBucket(rate_per_sec=0)
        with pytest.raises(ValueError):
            AsyncTokenBucket(rate_per_sec=-1)
