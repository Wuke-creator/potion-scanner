"""Stress test: simulate N verified users + K back-to-back alerts.

Runs the Dispatcher end-to-end against an in-memory fake DB and a mock
Telegram Bot. Measures throughput, per-alert duration, and failure rate.
No network, no real Telegram, no secrets required.

Usage:

    python -m scripts.stress_test --users 500 --alerts 5
    python -m scripts.stress_test --users 1000 --alerts 5 --rate 25
    python -m scripts.stress_test --users 2000 --alerts 10 --rate 25
    python -m scripts.stress_test --users 1000 --alerts 5 --failure-rate 0.05 \\
        --block-rate 0.02 --retry-after-rate 0.01

Flags:
    --users         Number of fake verified users (default: 500)
    --alerts        Number of back-to-back alerts to dispatch (default: 3)
    --rate          Dispatcher rate_per_sec cap (default: 25.0)
    --concurrent    Dispatcher max_concurrent workers (default: 25)
    --failure-rate  Fraction of users that hit a transient TelegramError (default: 0)
    --block-rate    Fraction of users that have blocked the bot (default: 0)
    --retry-after-rate  Fraction of sends that hit a RetryAfter (default: 0)
    --send-latency-ms   Simulated Telegram API latency per send (default: 30)

What it prints:
    - Config summary
    - Per-alert stats (sent / blocked / rate_limited / failed, duration)
    - Aggregate throughput (msgs/sec, total time)
    - Pass/fail on a simple throughput floor
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

from telegram.error import Forbidden, RetryAfter, TelegramError

from src.config import DispatcherConfig
from src.dispatcher import Dispatcher

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)
logger = logging.getLogger("stress_test")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeDB:
    active_ids: list[int] = field(default_factory=list)
    inactive_marks: list[int] = field(default_factory=list)

    async def list_active_user_ids(self) -> list[int]:
        return list(self.active_ids)

    async def update_after_recheck(
        self,
        telegram_user_id: int,
        is_active: bool,
        new_refresh_token_encrypted: str | None = None,
    ) -> None:
        if not is_active:
            self.inactive_marks.append(telegram_user_id)
            if telegram_user_id in self.active_ids:
                self.active_ids.remove(telegram_user_id)


class FakeBot:
    """Simulates Telegram send latency + configurable failure modes."""

    def __init__(
        self,
        send_latency_ms: int,
        failure_rate: float,
        block_rate: float,
        retry_after_rate: float,
        blocked_users: set[int],
    ):
        self._latency = send_latency_ms / 1000.0
        self._failure_rate = failure_rate
        self._block_rate = block_rate
        self._retry_after_rate = retry_after_rate
        self._blocked_users = blocked_users
        self._retried_once: set[int] = set()
        self.sent: list[int] = []
        self.send_call_count = 0

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.send_call_count += 1
        await asyncio.sleep(self._latency)

        if chat_id in self._blocked_users:
            raise Forbidden("user has blocked the bot")

        # RetryAfter: only trigger once per user so the dispatcher's retry
        # loop can succeed on the second attempt (realistic behavior).
        if (
            random.random() < self._retry_after_rate
            and chat_id not in self._retried_once
        ):
            self._retried_once.add(chat_id)
            raise RetryAfter(0.05)

        if random.random() < self._failure_rate:
            raise TelegramError("transient error")

        self.sent.append(chat_id)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


async def run_stress_test(args: argparse.Namespace) -> int:
    random.seed(42)

    user_ids = list(range(1_000_000, 1_000_000 + args.users))
    blocked_count = int(args.users * args.block_rate)
    blocked_users = set(random.sample(user_ids, blocked_count)) if blocked_count else set()

    db = FakeDB(active_ids=list(user_ids))
    bot = FakeBot(
        send_latency_ms=args.send_latency_ms,
        failure_rate=args.failure_rate,
        block_rate=args.block_rate,
        retry_after_rate=args.retry_after_rate,
        blocked_users=blocked_users,
    )

    config = DispatcherConfig(
        rate_per_sec=args.rate,
        max_concurrent=args.concurrent,
        per_send_timeout_sec=30.0,
        queue_max_size=max(args.alerts * 2, 100),
    )
    dispatcher = Dispatcher(bot=bot, db=db, config=config)

    print()
    print("=" * 72)
    print(f"Potion Signals Bot — Stress Test")
    print("=" * 72)
    print(f"  Users:              {args.users}")
    print(f"  Alerts:             {args.alerts}")
    print(f"  Rate cap:           {args.rate}/sec")
    print(f"  Max concurrent:     {args.concurrent}")
    print(f"  Simulated latency:  {args.send_latency_ms}ms per send")
    print(f"  Blocked users:      {blocked_count} ({args.block_rate:.1%})")
    print(f"  Failure rate:       {args.failure_rate:.1%}")
    print(f"  RetryAfter rate:    {args.retry_after_rate:.1%}")
    print()

    await dispatcher.start()

    total_start = time.monotonic()
    per_alert_stats = []
    try:
        # Fire off all alerts rapid-fire to simulate a cluster of calls
        for i in range(args.alerts):
            await dispatcher.dispatch(
                text=f"Alert {i + 1}/{args.alerts} — stress test payload",
                source_key=f"perp_bot#{i}",
            )

        # Wait for all alerts to drain (no direct API, so poll last_stats +
        # internal queue emptiness via bot.send_call_count)
        expected_min_sends = args.users - blocked_count
        # Each alert should land on every active user (blocked users raise).
        # We expect approximately users * alerts successful deliveries.
        while True:
            await asyncio.sleep(0.2)
            # Heuristic: every alert has been dispatched if bot's total
            # call_count reaches at least (users * alerts). Allow 20%
            # overshoot for RetryAfter retries.
            if bot.send_call_count >= expected_min_sends * args.alerts:
                # Give the dispatcher loop a moment to record stats
                await asyncio.sleep(0.3)
                break
    finally:
        await dispatcher.stop()

    total_elapsed = time.monotonic() - total_start

    # ----- Results -----
    print("Results:")
    print("-" * 72)
    stats = dispatcher.last_stats
    if stats is not None:
        print(
            f"  Last alert: sent={stats.sent}  blocked={stats.blocked}  "
            f"rate_limited={stats.rate_limited}  failed={stats.failed}  "
            f"duration={stats.duration_sec:.2f}s"
        )

    total_sends = len(bot.sent)
    total_call_attempts = bot.send_call_count
    expected_total = expected_min_sends * args.alerts
    throughput = total_sends / total_elapsed if total_elapsed > 0 else 0

    print(f"  Total send attempts: {total_call_attempts}")
    print(f"  Successful sends:    {total_sends} / ~{expected_total} expected")
    print(f"  Inactive marks:      {len(db.inactive_marks)}")
    print(f"  Total elapsed:       {total_elapsed:.2f}s")
    print(f"  Aggregate throughput: {throughput:.1f} msgs/sec")
    print()

    # ----- Simple pass/fail -----
    floor = args.rate * 0.60  # Expect to sustain at least 60% of rate cap
    success_ratio = total_sends / expected_total if expected_total else 1.0
    ok = throughput >= floor and success_ratio >= 0.80

    if ok:
        print(f"PASS: throughput {throughput:.1f}/s >= {floor:.1f}/s, "
              f"success {success_ratio:.1%} >= 80%")
        return 0
    else:
        print(f"FAIL: throughput {throughput:.1f}/s (floor {floor:.1f}/s), "
              f"success {success_ratio:.1%} (floor 80%)")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--users", type=int, default=500)
    parser.add_argument("--alerts", type=int, default=3)
    parser.add_argument("--rate", type=float, default=25.0)
    parser.add_argument("--concurrent", type=int, default=25)
    parser.add_argument("--failure-rate", type=float, default=0.0)
    parser.add_argument("--block-rate", type=float, default=0.0)
    parser.add_argument("--retry-after-rate", type=float, default=0.0)
    parser.add_argument("--send-latency-ms", type=int, default=30)
    args = parser.parse_args()
    return asyncio.run(run_stress_test(args))


if __name__ == "__main__":
    raise SystemExit(main())
