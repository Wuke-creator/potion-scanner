"""Tests for the suppression-rate monitor in src/email_bot/suppression_metrics.

Covers:
  - rate arithmetic (snapshot)
  - sliding-window pruning
  - alert fires above threshold, doesn't fire below
  - alert cooldown prevents repeat fires
  - min_attempts floor prevents spurious alarms in low-volume windows
  - webhook POST is best-effort and never raises
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiohttp import web

from src.email_bot.suppression_metrics import (
    DEFAULT_ALERT_COOLDOWN_SECONDS,
    SuppressionMetrics,
)


# ---------------------------------------------------------------------------
# Clock fixture
# ---------------------------------------------------------------------------


class _Clock:
    """Deterministic clock for tests. Tick to advance."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Pure state tests
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_empty_metrics_has_zero_rate(self):
        m = SuppressionMetrics(clock=_Clock())
        snap = m.snapshot()
        assert snap["attempted"] == 0
        assert snap["suppressed"] == 0
        assert snap["rate"] == 0.0

    def test_attempts_only_zero_rate(self):
        m = SuppressionMetrics(clock=_Clock())
        for _ in range(50):
            m.record_attempt()
        snap = m.snapshot()
        assert snap["attempted"] == 50
        assert snap["suppressed"] == 0
        assert snap["rate"] == 0.0

    def test_some_suppressed(self):
        m = SuppressionMetrics(
            clock=_Clock(), webhook_url="", min_attempts=1, threshold=0.99,
        )
        for _ in range(100):
            m.record_attempt()
        # 10 of those also count as suppressions (attempt + suppression
        # together; in the real worker the call order is attempt-then-
        # suppression for blocked sends).
        for _ in range(10):
            m.record_suppression("hard bounce")
        snap = m.snapshot()
        # 100 attempts, 10 suppressions = 10%
        assert snap["attempted"] == 100
        assert snap["suppressed"] == 10
        assert snap["rate"] == pytest.approx(0.1)

    def test_top_reasons_capped_at_5(self):
        m = SuppressionMetrics(
            clock=_Clock(), webhook_url="", min_attempts=1, threshold=2.0,
        )
        for _ in range(100):
            m.record_attempt()
        # Seven distinct reasons -- top 5 should win.
        for r in ("a", "b", "c", "d", "e", "f", "g"):
            m.record_suppression(r)
        snap = m.snapshot()
        assert len(snap["top_reasons"]) == 5


class TestSlidingWindow:
    def test_old_events_pruned(self):
        clk = _Clock()
        m = SuppressionMetrics(
            clock=clk, window_seconds=3600, webhook_url="",
            min_attempts=1, threshold=2.0,
        )
        # Record 100 old attempts -- they should evaporate after the window.
        for _ in range(100):
            m.record_attempt()
        assert m.snapshot()["attempted"] == 100

        clk.advance(3601)  # one second past the window
        # New event triggers a prune.
        m.record_attempt()
        snap = m.snapshot()
        assert snap["attempted"] == 1

    def test_within_window_retained(self):
        clk = _Clock()
        m = SuppressionMetrics(
            clock=clk, window_seconds=3600, webhook_url="",
            min_attempts=1, threshold=2.0,
        )
        for _ in range(10):
            m.record_attempt()
            clk.advance(60)  # 1 minute apart
        # All ten should still be in-window after 9 minutes of advance.
        assert m.snapshot()["attempted"] == 10


# ---------------------------------------------------------------------------
# Alert threshold logic
# ---------------------------------------------------------------------------


class TestAlertThreshold:
    def test_alert_fires_above_threshold(self):
        m = SuppressionMetrics(
            clock=_Clock(), threshold=0.05, min_attempts=20, webhook_url="",
            alert_cooldown_seconds=0,
        )
        # 20 attempts, 2 suppressed -> 10% > 5% threshold
        for _ in range(20):
            m.record_attempt()
        for _ in range(2):
            m.record_suppression("hard bounce")
        assert m.check_alert_threshold() is True

    def test_alert_does_not_fire_below_threshold(self):
        m = SuppressionMetrics(
            clock=_Clock(), threshold=0.05, min_attempts=20, webhook_url="",
        )
        # 100 attempts, 1 suppressed -> 1% < 5%
        for _ in range(100):
            m.record_attempt()
        m.record_suppression("hard bounce")
        assert m.check_alert_threshold() is False

    def test_alert_does_not_fire_below_min_attempts(self):
        # Even at 100% suppression, if attempts < min the alert stays
        # silent. Critical for overnight quiet hours.
        m = SuppressionMetrics(
            clock=_Clock(), threshold=0.05, min_attempts=20, webhook_url="",
        )
        for _ in range(5):
            m.record_attempt()
            m.record_suppression("hard bounce")
        snap = m.snapshot()
        assert snap["rate"] == 1.0  # 5/5
        assert m.check_alert_threshold() is False  # but below min_attempts

    def test_cooldown_prevents_repeat_fires(self):
        clk = _Clock()
        m = SuppressionMetrics(
            clock=clk, threshold=0.05, min_attempts=20, webhook_url="",
            alert_cooldown_seconds=300,
        )
        for _ in range(20):
            m.record_attempt()
        # record_suppression internally calls _maybe_alert which sets the
        # cooldown the moment the first qualifying breach happens. After
        # that, check_alert_threshold returns False until the cooldown
        # expires, even though the rate is still above threshold.
        m.record_suppression("hard bounce")
        m.record_suppression("hard bounce")
        # Cooldown is now active because _maybe_alert ran inside the
        # first record_suppression call.
        assert m.check_alert_threshold() is False
        # After advancing past the cooldown, the next check fires again.
        clk.advance(301)
        assert m.check_alert_threshold() is True


# ---------------------------------------------------------------------------
# Webhook POST behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestWebhookPosting:
    async def test_webhook_post_on_threshold_breach(self, tmp_path):
        """A real local HTTP server captures the POST and asserts the
        body shape."""
        received: list[dict] = []

        async def _handler(request: web.Request) -> web.Response:
            payload = await request.json()
            received.append(payload)
            return web.Response(status=200, text="ok")

        app = web.Application()
        app.router.add_post("/webhook", _handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        # Resolve the actual port.
        port = site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/webhook"

        try:
            m = SuppressionMetrics(
                threshold=0.05, min_attempts=10,
                webhook_url=url,
                alert_cooldown_seconds=0,
            )
            for _ in range(20):
                m.record_attempt()
            # Three suppressions over 20 attempts = 15% > 5% threshold.
            # The first record_suppression triggers the alert; the rest
            # are absorbed by the same fire-and-forget cycle (or a fresh
            # one with cooldown=0, which doesn't matter for capturing
            # at least one POST).
            for _ in range(3):
                m.record_suppression("hard bounce")
            # Give the fire-and-forget task time to land.
            for _ in range(20):
                if received:
                    break
                await asyncio.sleep(0.05)
            assert received
            payload = received[0]
            assert "content" in payload
            assert "suppression" in payload["content"].lower()
            assert "hard bounce" in payload["content"]
        finally:
            await runner.cleanup()

    async def test_webhook_post_failure_is_non_blocking(self):
        """If the webhook URL is unreachable / returns 5xx, the metric
        keeps recording and never raises into the caller."""
        m = SuppressionMetrics(
            threshold=0.05, min_attempts=1,
            # Use an obviously-bad port; even with kernel deny the
            # POST raises connection refused which our handler swallows.
            webhook_url="http://127.0.0.1:1/dead",
            alert_cooldown_seconds=0,
        )
        for _ in range(5):
            m.record_attempt()
        # Should not raise even though the webhook is down.
        m.record_suppression("hard bounce")
        # Give the fire-and-forget task a moment to fail.
        await asyncio.sleep(0.1)
        # Metric state is unaffected: counts still went up.
        snap = m.snapshot()
        assert snap["attempted"] == 5
        assert snap["suppressed"] == 1

    async def test_no_webhook_url_means_log_only(self, caplog):
        """When webhook_url is empty, the WARNING still logs but no
        POST is attempted. The class never raises."""
        import logging

        caplog.set_level(logging.WARNING, "src.email_bot.suppression_metrics")
        m = SuppressionMetrics(
            threshold=0.05, min_attempts=1, webhook_url="",
            alert_cooldown_seconds=0,
        )
        for _ in range(20):
            m.record_attempt()
        # 3 suppressions / 20 attempts = 15% > 5% threshold.
        for _ in range(3):
            m.record_suppression("hard bounce")
        # The WARNING line landed.
        assert any(
            "suppression rate elevated" in r.message.lower()
            for r in caplog.records
        )
