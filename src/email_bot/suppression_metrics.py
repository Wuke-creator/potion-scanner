"""Suppression-rate monitoring for the EmailWorker.

Tracks a sliding 1-hour window of ``email_attempted`` vs
``email_suppressed`` counts and fires a WARNING + optional Discord
webhook alert when the suppression rate exceeds the configured
threshold (default 5%).

Why this exists:
    The last-mile suppression gate (``EmailWorker._deliver_one``)
    catches recipients with prior bounces / complaints / unsubscribes
    even after their row slips through the webhook handler's
    cancel_all_pending. That's good defense in depth, but it also
    masks problems: if 50% of our sends are getting suppressed at the
    gate, something upstream is broken (Whop sync stale,
    cancel_all_pending failing silently, mass-bounce event). We need
    an alarm.

Design:
    - Tiny in-memory deque of (timestamp, kind, reason) tuples.
    - Pruned on every record_* call -- no background task.
    - check_alert_threshold() runs after every suppression record so
      we get a fresh decision without an external poll.
    - Cooldown between alerts (default 15 min) so a stuck pipeline
      doesn't spam #ops-alerts every send.
    - Webhook POST is best-effort: 5s timeout, exceptions logged not
      raised. The worker keeps delivering whether the alert lands or
      not.

The class is intentionally synchronous about state mutation (deque
operations) and asynchronous only about the webhook POST. The worker
holds one instance per process; it's safe under asyncio because each
mutator is short and only one delivery loop runs concurrently.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, deque
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


# Default alarm threshold. Tune via the constructor if 5% is too tight
# (e.g. during a known bad-list cleanup) or too loose. Industry SaaS
# benchmark: anything north of 2-3% sustained suppression usually
# signals a list-hygiene problem, so 5% is comfortably "things are
# definitely on fire" without being so high it trips on minor noise.
DEFAULT_SUPPRESSION_THRESHOLD = 0.05

# Window over which the rate is computed. One hour matches the typical
# operator-attention span and gives the deque a bounded memory ceiling
# even at high send rates (1.5 sends/sec * 3600s = 5400 entries).
DEFAULT_WINDOW_SECONDS = 3600

# Minimum attempts before the threshold fires. Without this, a single
# suppressed send in a quiet hour would be 100% suppression rate and
# fire instantly. 20 is a reasonable floor: at 1.5 sends/sec it's
# ~14 seconds of traffic, but in low-volume periods (overnight) it
# means we don't alarm on noise.
DEFAULT_MIN_ATTEMPTS = 20

# Cooldown between alert fires. Without this, every subsequent
# suppression during a stuck pipeline would re-fire the webhook. 15
# minutes is enough for an on-call human to ack-and-investigate
# without losing visibility into a fast-deteriorating situation.
DEFAULT_ALERT_COOLDOWN_SECONDS = 15 * 60


class SuppressionMetrics:
    """Sliding-window suppression-rate tracker with optional webhook
    alerting.

    All state is in-process; restart resets the window. That's fine:
    the EmailWorker is a single long-running task and the metric is
    operational, not historical (the events_db has the history).

    Public API:
      - record_attempt() : count one delivery attempt
      - record_suppression(reason) : count one suppressed send + trigger
        the alert check
      - check_alert_threshold() : explicit re-check (for tests)
      - snapshot() : return the current attempted/suppressed/rate state
    """

    def __init__(
        self,
        *,
        threshold: float = DEFAULT_SUPPRESSION_THRESHOLD,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        min_attempts: int = DEFAULT_MIN_ATTEMPTS,
        webhook_url: str = "",
        alert_cooldown_seconds: int = DEFAULT_ALERT_COOLDOWN_SECONDS,
        clock: Optional[callable] = None,
        http_session: Optional[aiohttp.ClientSession] = None,
    ):
        self._threshold = float(threshold)
        self._window = int(window_seconds)
        self._min_attempts = int(min_attempts)
        self._webhook_url = (webhook_url or "").strip()
        self._alert_cooldown = int(alert_cooldown_seconds)
        # Time source: injectable for deterministic tests.
        self._now = clock if clock is not None else time.time
        # Aiohttp session: optional injection. When None, we create a
        # short-lived one per POST (used in production where the worker
        # doesn't run an HTTP server).
        self._session = http_session
        # (timestamp, kind, reason) where kind is "attempt" | "suppressed"
        # Storing kind explicitly so we can prune both kinds against the
        # same window without two structures going out of sync.
        self._events: deque[tuple[float, str, str]] = deque()
        # Last successful alert fire (epoch seconds). Initialised to 0
        # so the first qualifying breach always fires.
        self._last_alert_at: float = 0.0

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_attempt(self) -> None:
        """Count one delivery attempt. Always pair with a record_suppression
        or a successful send -- the rate denominator is just attempts."""
        self._prune()
        self._events.append((self._now(), "attempt", ""))

    def record_suppression(self, reason: str) -> None:
        """Count one suppressed send and re-check the alert threshold.

        ``reason`` should be a short string ("hard bounce", "complaint",
        "unsubscribed", "suppression check error"). It's used in the
        alert message to point on-call at the dominant cause.
        """
        self._prune()
        self._events.append((self._now(), "suppressed", str(reason or "")))
        # Fire-and-forget alert check. We don't await here because
        # record_suppression is called from inside the delivery loop and
        # we'd rather not block on a slow webhook. If asyncio isn't
        # available (sync test context), schedule_alert silently no-ops.
        self._maybe_alert()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _prune(self) -> None:
        """Drop entries older than the configured window."""
        cutoff = self._now() - self._window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def snapshot(self) -> dict:
        """Current state. Used by /engagement-snapshot, tests, and the
        alert builder."""
        self._prune()
        attempted = 0
        suppressed = 0
        reasons: Counter[str] = Counter()
        for _, kind, reason in self._events:
            if kind == "attempt":
                attempted += 1
            elif kind == "suppressed":
                suppressed += 1
                if reason:
                    reasons[reason] += 1
        rate = (suppressed / attempted) if attempted else 0.0
        return {
            "attempted": attempted,
            "suppressed": suppressed,
            "rate": rate,
            "window_seconds": self._window,
            "top_reasons": reasons.most_common(5),
            "threshold": self._threshold,
            "min_attempts": self._min_attempts,
        }

    # ------------------------------------------------------------------
    # Alerting
    # ------------------------------------------------------------------

    def check_alert_threshold(self) -> bool:
        """Return True if the current rate breaches the threshold AND
        we're past the cooldown window. Pure: does not fire the alert
        itself; use _maybe_alert() or call from a test asserting state.
        """
        snap = self.snapshot()
        if snap["attempted"] < self._min_attempts:
            return False
        if snap["rate"] <= self._threshold:
            return False
        if (self._now() - self._last_alert_at) < self._alert_cooldown:
            return False
        return True

    def _maybe_alert(self) -> None:
        """Decide whether to fire an alert and dispatch it.

        Always logs a WARNING when the threshold is breached, even if
        the webhook isn't configured. The webhook POST is scheduled as
        a background task so the delivery loop never blocks on it.
        """
        if not self.check_alert_threshold():
            return
        snap = self.snapshot()
        # Mark the cooldown so a flurry of suppressions in the same
        # breach doesn't fire repeatedly. Done before the (async) POST
        # so even a slow webhook can't cause duplicate fires.
        self._last_alert_at = self._now()
        # Format reasons for the warning line.
        reasons_str = ", ".join(
            f"{r}={c}" for r, c in snap["top_reasons"]
        ) or "(none recorded)"
        logger.warning(
            "Email suppression rate elevated: %d/%d = %.1f%% over last %ds "
            "(threshold %.1f%%). Top reasons: %s",
            snap["suppressed"], snap["attempted"], snap["rate"] * 100,
            snap["window_seconds"], snap["threshold"] * 100, reasons_str,
        )
        if self._webhook_url:
            self._schedule_webhook(snap)

    def _schedule_webhook(self, snap: dict) -> None:
        """Schedule the webhook POST as an asyncio task so the caller
        doesn't block. Silently no-ops outside an event loop (e.g.
        sync test contexts) -- the WARNING log line is still emitted."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop. The warning already logged; nothing else
            # we can do without one.
            return
        loop.create_task(
            self._post_webhook(snap),
            name="suppression_alert_webhook",
        )

    async def _post_webhook(self, snap: dict) -> None:
        """POST the alert to the configured Discord webhook. Never raises.

        Discord webhooks accept JSON bodies with a ``content`` (plain
        text) field. We keep the message short and put the reason
        breakdown on its own lines for skimmability.
        """
        try:
            reasons_lines = "\n".join(
                f"  - {r}: {c}" for r, c in snap["top_reasons"]
            ) or "  - (none recorded)"
            content = (
                f"**Email suppression rate elevated**\n"
                f"Rate: {snap['rate'] * 100:.1f}% "
                f"({snap['suppressed']}/{snap['attempted']} in last "
                f"{snap['window_seconds'] // 60}m)\n"
                f"Threshold: {snap['threshold'] * 100:.1f}%\n"
                f"Top suppression reasons:\n{reasons_lines}"
            )
            timeout = aiohttp.ClientTimeout(total=5)
            payload = {"content": content[:1900]}  # Discord 2000-char cap
            if self._session is not None:
                async with self._session.post(
                    self._webhook_url,
                    json=payload,
                    timeout=timeout,
                ) as resp:
                    if resp.status >= 400:
                        logger.warning(
                            "Suppression alert webhook returned HTTP %d",
                            resp.status,
                        )
            else:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        self._webhook_url, json=payload,
                    ) as resp:
                        if resp.status >= 400:
                            logger.warning(
                                "Suppression alert webhook returned HTTP %d",
                                resp.status,
                            )
        except Exception:
            # Webhook delivery failures must not bubble back to the
            # delivery loop. Log and move on.
            logger.exception(
                "Suppression alert webhook post crashed (non-blocking)",
            )
