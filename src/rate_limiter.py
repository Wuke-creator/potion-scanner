"""Async token-bucket rate limiter.

Telegram's global bot API limit is ~30 messages per second across all
chats. We run the dispatcher at a configurable rate (default 25/sec) to
leave headroom for the reverify cron and user command replies.

The bucket holds up to ``capacity`` tokens and refills at ``rate_per_sec``.
``take(n)`` blocks until ``n`` tokens are available, then deducts them.
Safe for concurrent use from multiple workers in the same event loop.
"""

from __future__ import annotations

import asyncio
import time


class AsyncTokenBucket:
    """Leaky token bucket, concurrency-safe within one asyncio event loop."""

    def __init__(self, rate_per_sec: float, capacity: int | None = None):
        if rate_per_sec <= 0:
            raise ValueError(f"rate_per_sec must be > 0, got {rate_per_sec}")
        self._rate = float(rate_per_sec)
        self._capacity = float(capacity if capacity is not None else max(rate_per_sec, 1.0))
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self, n: int = 1) -> None:
        """Block until ``n`` tokens are available, then deduct them."""
        if n <= 0:
            return
        if n > self._capacity:
            raise ValueError(
                f"cannot take {n} tokens from a bucket with capacity {self._capacity}"
            )
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                if elapsed > 0:
                    self._tokens = min(
                        self._capacity, self._tokens + elapsed * self._rate,
                    )
                    self._last_refill = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                deficit = n - self._tokens
                wait = deficit / self._rate
            # Release the lock while sleeping so other callers can progress
            # as soon as their own tokens are ready.
            await asyncio.sleep(wait)

    @property
    def available(self) -> float:
        """Current token count. Useful for diagnostics / stress tests."""
        return self._tokens
