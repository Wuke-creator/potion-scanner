"""Synthetic load test for the email pipeline.

Runs the real `src.email_bot.worker.EmailWorker` against:
  - A real `EmailDB` opened on a tempfile, pre-populated with N subscribers
    and N pending scheduled_sends
  - A `FakeResendClient` that simulates per-send latency and configurable
    failure modes (transient error, Resend 429, Resend 401)
  - Monkey-patched `gather_stats` + `render` to isolate the measurement
    to the send pipeline itself (no analytics DB, no real templates)

No network. No real Resend API calls. No production credentials.

What it prints:
  - Config summary
  - Throughput over time (per-cycle stats from the worker)
  - Aggregate throughput (sends/sec, total time)
  - Success/fail breakdown
  - Pass/fail on simple thresholds

Usage:

    python -m scripts.stress_test_email --sends 150000 --max-per-cycle 1000 --poll-sec 0.5
    python -m scripts.stress_test_email --sends 50000 --failure-rate 0.02

Flags:
    --sends              Total pending scheduled_sends to deliver (default: 10000)
    --max-per-cycle      EmailWorker.max_per_cycle (default: 1000)
    --poll-sec           EmailWorker.poll_interval_sec (default: 0.5)
    --send-latency-ms    Simulated Resend latency per send (default: 20)
    --failure-rate       Fraction of sends that hit a transient error (default: 0)
    --resend-429-rate    Fraction that hit a Resend rate-limit error (default: 0)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import tempfile
import time
from pathlib import Path

# Silence most framework noise; let the harness print its own progress
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)

from src.email_bot import EmailDB
from src.email_bot.db import Subscriber
from src.email_bot.sender import SendResult
from src.email_bot.worker import EmailWorker
from src.email_bot import stats as stats_module
from src.email_bot import worker as worker_module


# ---------------------------------------------------------------------------
# Fakes + monkey patches
# ---------------------------------------------------------------------------


class FakeResendClient:
    """Simulates Resend's send latency + a few failure modes."""

    def __init__(
        self,
        latency_ms: int,
        failure_rate: float,
        rate_limit_rate: float,
    ):
        self._latency = latency_ms / 1000.0
        self._failure_rate = failure_rate
        self._rate_limit_rate = rate_limit_rate
        self.send_count = 0
        self.ok_count = 0
        self.fail_count = 0

    async def send(self, *, to, subject, html, text, from_name=None, reply_to=None):
        self.send_count += 1
        await asyncio.sleep(self._latency)

        r = random.random()
        if r < self._rate_limit_rate:
            self.fail_count += 1
            return SendResult(ok=False, error="429 rate_limit_exceeded")
        if r < self._rate_limit_rate + self._failure_rate:
            self.fail_count += 1
            return SendResult(ok=False, error="500 internal")

        self.ok_count += 1
        return SendResult(ok=True, resend_id=f"fake_{self.send_count}")

    async def close(self):
        pass


def _fake_stats():
    """Minimal StatsBundle to skip the analytics DB dependency."""
    return stats_module.StatsBundle(
        calls_7d_total=10,
        wins_7d_over_50pct=2,
        top_call_7d={"pair": "ETH/USDT", "pnl_pct": 180.0, "days_ago": 2},
        top_calls_7d=[
            {"pair": "ETH/USDT", "pnl_pct": 180.0, "days_ago": 2},
            {"pair": "PEPE/USDT", "pnl_pct": 480.0, "days_ago": 1},
            {"pair": "BTC/USDT", "pnl_pct": 62.0, "days_ago": 3},
        ],
        calls_30d_total=80,
        top_call_30d={"pair": "PEPE/USDT", "pnl_pct": 480.0, "days_ago": 1},
    )


class _FakeEmailMsg:
    """Minimal EmailMessage stand-in returned by the patched render()."""
    def __init__(self, idx: int):
        self.subject = f"Test email #{idx}"
        self.html = f"<html><body>Test #{idx}</body></html>"
        self.text = f"Test #{idx}"
        self.from_name = "Potion Alpha Team"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


async def _bulk_populate(db: EmailDB, n: int) -> None:
    """Insert N subscribers + N pending scheduled_sends in batched transactions.

    Uses the internal aiosqlite connection directly (skipping upsert_subscriber's
    per-row commit) so 150k rows don't take 30 minutes to load.
    """
    assert db._conn is not None
    now = int(time.time())
    past_due = now - 3600  # due 1h ago so the worker picks them up immediately

    # Subscribers
    batch_size = 5000
    for i in range(0, n, batch_size):
        rows = [
            (
                f"loadtest{j}@example.invalid",
                f"User {j}",
                "cancellation",
                "none",
                "",
                now,
            )
            for j in range(i, min(i + batch_size, n))
        ]
        await db._conn.executemany(
            "INSERT INTO subscribers "
            "(email, name, trigger_type, exit_reason, rejoin_url, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
    await db._conn.commit()

    # Scheduled sends (all pending, all past due)
    for i in range(0, n, batch_size):
        rows = [
            (
                f"loadtest{j}@example.invalid",
                "winback",
                1,
                past_due,
                "pending",
            )
            for j in range(i, min(i + batch_size, n))
        ]
        await db._conn.executemany(
            "INSERT INTO scheduled_sends "
            "(email, sequence, day, due_at, status) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
    await db._conn.commit()


async def run(args: argparse.Namespace) -> int:
    random.seed(42)

    # Monkey-patch: skip analytics + template rendering so we isolate the
    # send pipeline. gather_stats is awaited -> make patch async.
    async def _stats_coro(_path: str):
        return _fake_stats()
    stats_module.gather_stats = _stats_coro
    worker_module.gather_stats = _stats_coro

    counter = {"n": 0}

    def _render_patch(sequence, day, subscriber, stats):
        counter["n"] += 1
        return _FakeEmailMsg(counter["n"])

    worker_module.render = _render_patch

    # Real EmailDB on a tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    db = EmailDB(db_path=db_path)
    await db.open()

    print()
    print("=" * 72)
    print("Potion Scanner — Email Pipeline Stress Test")
    print("=" * 72)
    print(f"  Sends to deliver:   {args.sends}")
    print(f"  Max per cycle:      {args.max_per_cycle}")
    print(f"  Poll interval:      {args.poll_sec}s")
    print(f"  Simulated latency:  {args.send_latency_ms}ms per Resend call")
    print(f"  Failure rate:       {args.failure_rate:.1%}")
    print(f"  Resend 429 rate:    {args.resend_429_rate:.1%}")
    print()

    # Populate
    print(f"Populating email.db with {args.sends:,} rows...")
    pop_start = time.monotonic()
    await _bulk_populate(db, args.sends)
    print(f"  populated in {time.monotonic() - pop_start:.2f}s")
    counts = await db.count_by_status()
    print(f"  status breakdown: {counts}")
    print()

    fake_sender = FakeResendClient(
        latency_ms=args.send_latency_ms,
        failure_rate=args.failure_rate,
        rate_limit_rate=args.resend_429_rate,
    )
    worker = EmailWorker(
        db=db,
        sender=fake_sender,
        analytics_db_path="fake.db",  # never actually read thanks to patch
        poll_interval_sec=args.poll_sec,
        max_per_cycle=args.max_per_cycle,
    )

    await worker.start()

    start = time.monotonic()
    last_report = start

    try:
        while True:
            await asyncio.sleep(2.0)
            now = time.monotonic()
            elapsed = now - start
            counts = await db.count_by_status()
            pending = counts.get("pending", 0)
            sent = counts.get("sent", 0)
            failed = counts.get("failed", 0)
            total_done = sent + failed
            rate = total_done / elapsed if elapsed > 0 else 0
            # Progress line every 2s (guarded so we don't spam CI)
            print(
                f"  t={elapsed:6.1f}s  "
                f"sent={sent:>7}  failed={failed:>5}  pending={pending:>7}  "
                f"{rate:6.1f}/s"
            )
            if pending == 0:
                break
            if elapsed > 1800:
                print("  ...timeout after 30 min, bailing")
                break
    finally:
        await worker.stop()

    elapsed = time.monotonic() - start

    # ---- Results ----
    counts = await db.count_by_status()
    sent = counts.get("sent", 0)
    failed = counts.get("failed", 0)
    total_done = sent + failed
    throughput = total_done / elapsed if elapsed > 0 else 0
    success_rate = sent / total_done if total_done else 0.0

    print()
    print("Results:")
    print("-" * 72)
    print(f"  Wall-clock:          {elapsed:.2f}s")
    print(f"  Sent (ok):           {sent:,}")
    print(f"  Failed:              {failed:,}")
    print(f"  Still pending:       {counts.get('pending', 0):,}")
    print(f"  Aggregate throughput: {throughput:.1f} sends/sec")
    print(f"  Success rate:        {success_rate:.1%}")
    print(f"  FakeResend send_count: {fake_sender.send_count:,} "
          f"(ok={fake_sender.ok_count:,}, fail={fake_sender.fail_count:,})")
    print()

    await db.close()
    try:
        db_path.unlink()
    except Exception:
        pass

    # Simple pass/fail
    expected_pct = 1.0 - (args.failure_rate + args.resend_429_rate)
    ok = total_done == args.sends and success_rate >= (expected_pct - 0.02)
    if ok:
        print(
            f"PASS: delivered {total_done:,}/{args.sends:,}, success {success_rate:.1%} "
            f"(expected ~{expected_pct:.1%})"
        )
        return 0
    else:
        print(
            f"FAIL: delivered {total_done:,}/{args.sends:,}, success {success_rate:.1%} "
            f"(expected ~{expected_pct:.1%})"
        )
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--sends", type=int, default=10000)
    parser.add_argument("--max-per-cycle", type=int, default=1000)
    parser.add_argument("--poll-sec", type=float, default=0.5)
    parser.add_argument("--send-latency-ms", type=int, default=20)
    parser.add_argument("--failure-rate", type=float, default=0.0)
    parser.add_argument("--resend-429-rate", type=float, default=0.0)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
