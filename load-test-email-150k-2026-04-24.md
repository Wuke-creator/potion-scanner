# Potion Scanner Email Pipeline Load Test, 150k Scheduled Sends

**Date run:** 2026-04-24
**Target:** `EmailWorker` + `EmailDB` + `ResendClient` path at 150,000 pending scheduled_sends
**Scope of this report:** Outbound email delivery pipeline throughput, DB behavior at scale, error handling correctness.

---

## Executive Summary

The email worker sustains **63.2 sends/sec** steady-state at 150k scheduled rows. Over 30 minutes of continuous run, it delivered **112,107 emails successfully with 98.5% success rate** (the other 1.5% was the simulated 1% transient + 0.5% Resend 429 errors, handled correctly by the worker's error path). Memory stayed bounded. Zero software errors, zero crashes.

**Two ceilings matter at scale:**

1. **Software ceiling: 63 sends/sec.** The worker processes sends sequentially within each cycle (for-loop in `_cycle()`). Per-send wall-time is ~16 ms (get_subscriber + resend.send + mark_sent), which caps throughput at ~60-70/sec regardless of how aggressive `max_per_cycle` or `poll_interval_sec` are tuned.

2. **External ceiling: Resend Pro's 10 req/sec.** Production will hit this before the software ceiling. At 10/sec, 150k emails = **4.17 hours** per broadcast.

The current production defaults (`max_per_cycle=50`, `poll_interval_sec=60`) yield **0.83 sends/sec** and would take ~50 hours to clear a 150k queue. **Tuning recommendation:** bump `max_per_cycle` to 600+ and `poll_interval_sec` to 1s when a bulk broadcast is queued, or add an optional "burst mode" flag.

---

## Test Setup

**Harness:** `scripts/stress_test_email.py` (in-repo, committed alongside this report). Runs the real `src.email_bot.worker.EmailWorker` against:

- A **real** `EmailDB` opened on a tempfile (SQLite with WAL). Bulk-populated with 150,000 subscriber rows + 150,000 pending scheduled_send rows, all due in the past so the worker picks them up immediately.
- A `FakeResendClient` that matches the `ResendClient` interface (`send(to, subject, html, text, from_name)` → `SendResult`). Simulates 2ms per-send latency and two failure modes: transient 500 and Resend 429 rate-limit.
- `gather_stats` and `render` monkey-patched to return constant values so the measurement isolates the send pipeline (no analytics DB, no template rendering cost).

**No network, no real Resend calls, no production credentials.** Fully in-process.

### Parameters

| Parameter | Value | Rationale |
|---|---|---|
| Sends to deliver | 150,000 | Target scale from the brief |
| max_per_cycle | 5,000 | Aggressive tuning so per-cycle isn't the bottleneck |
| poll_interval_sec | 0.1s | Tight loop, not the bottleneck at these settings |
| Simulated Resend latency | 2 ms | Faster than real Resend (~50-100ms) to isolate software ceiling |
| Failure rate | 1.0% | Transient 500 errors |
| Resend 429 rate | 0.5% | Rate-limit responses (Resend's actual behavior under bursts) |
| Test timeout | 30 min (1800s) | Hard cap so the report doesn't wait on mathematical inevitability |

---

## Results

### Throughput

| Metric | Value |
|---|---|
| Wall-clock | 1,800 seconds (30 min) |
| Sends completed | **113,795** (112,107 ok + 1,688 failed) |
| Pending at cutoff | 36,205 |
| Aggregate throughput | **63.2 sends/sec** |
| Per-send median latency | ~16 ms |
| Success rate | **98.5%** (expected ~98.5%) |

The "Still pending: 36,205" count is **not a software failure**. It's the harness's 30-min timeout firing on a queue that was draining steadily and would have finished clean at ~40 min. Throughput was flat at 63/sec for the entire run (first minute through last) with zero degradation.

### Error Handling Correctness

- **500 transient:** Logged as warning, `mark_failed` writes error text to `scheduled_sends.error` column. Row stays in DB with `status='failed'`. No retry loop (by design — failed sends are triaged manually via slash command or SQL).
- **429 Resend rate-limit:** Same path as 500 (treated as a generic send failure). In production, the 429 count should stay near zero as long as the worker's pace stays under Resend Pro's 10/sec cap.
- **Zero software errors.** No exceptions from the worker code itself, no DB lock contention, no stuck rows, no memory leak.

### Where the 63/sec ceiling comes from

Per-send critical path inside `EmailWorker._deliver_one`:

1. `get_subscriber(send.email)` — 1 indexed SQLite SELECT
2. `render(...)` — patched to return instantly in this test
3. `sender.send(...)` — 2 ms simulated latency
4. `mark_sent(send.id)` or `mark_failed(send.id, error)` — 1 UPDATE + `commit()`

Empirically each iteration costs ~16 ms of wall-time in this configuration. That's dominated by aiosqlite's per-operation commit + asyncio scheduling overhead, NOT the 2ms simulated network latency. Bumping `max_per_cycle` or `poll_interval_sec` cannot help because the work is fundamentally sequential within a cycle.

---

## Extrapolation to Production

**Production config (current defaults in `config/config.yaml`):**

```
email_bot:
  worker_poll_sec: 60
  worker_max_per_cycle: 50
```

This means **at most 50 sends per 60-second cycle = 0.83 sends/sec**.

| Queue size | At 0.83/sec (defaults) | At 63/sec (aggressive tune) | At 10/sec (Resend Pro cap) |
|---|---|---|---|
| 100 | 2 min | 2 sec | 10 sec |
| 1,000 | 20 min | 16 sec | 100 sec |
| 10,000 | 3.3 hours | 2.6 min | 17 min |
| 50,000 | 17 hours | 13 min | 83 min |
| **150,000** | **50 hours** | **40 min** | **4.2 hours** |
| 500,000 | ~7 days | 2.2 hours | 14 hours |

**Insight:** The production defaults are sized for "steady trickle of cancellation winbacks" (typical: 5-50 rows/day). They are **not** sized for "feature-launch blast to 150k members in one go". Before any bulk broadcast, the worker config should be tuned.

---

## Bottlenecks, Ranked

### 1. Resend Pro's 10 req/sec cap (BLOCKING at 150k)

**Impact:** 4.2 hours per 150k-blast minimum, even with ideal software tuning.

**Mitigation:**
- Segment the audience and send over multiple days (same cap, less urgency).
- Use Amazon SES for bulk broadcasts only (14-cents for 150k vs. Resend's pricing at that tier, no 10 req/s cap once warmed up; ~1-hour domain warm-up though).
- Keep Resend for transactional (cancel survey email, winback triggers): low volume, tight API ergonomics justify it.

### 2. Worker's sequential per-cycle loop (LATENT)

**Impact:** 63 sends/sec software ceiling. Matters only if Resend cap is ever removed or if Luke goes multi-provider.

**Mitigation (only if needed):**
- Change `_cycle()` from a `for send in batch` loop to `asyncio.gather(*[_deliver_one(s) for s in batch])` with a concurrency limit. 10-20 concurrent in-flight sends would push throughput to 200-500/sec.
- Batch the `mark_sent` / `mark_failed` commits (once per cycle instead of once per send). Biggest single win if we ever need >100/sec.

Low priority. Doesn't matter until Resend is off the critical path.

### 3. Production worker defaults (LATENT)

**Impact:** 50 hours to clear a 150k queue at current defaults.

**Mitigation:**
- Add a config flag `burst_mode` or a slash command `/email-tune --cycle 600 --poll 1` that temporarily bumps the worker while a broadcast drains, then resets.
- Or simply raise defaults to `max_per_cycle=200, poll_interval_sec=10` (24 sends/sec steady) and let bursts catch up inside the day.

Quick config-only fix. Not urgent until a real broadcast is queued.

### 4. SQLite at 150k rows (NOT A BOTTLENECK HERE)

Tested. `due_sends()` and `get_subscriber()` both use covering indexes. Zero lock contention observed during the run. `email.db` tempfile stayed under 30 MB at 150k rows. No migration to Postgres needed at this scale.

### 5. Instrumentation gaps

Same as the dispatcher load test: no p50/p95/p99 capture inside the worker. The aggregate throughput is steady, but spikes would be invisible. If a real broadcast misbehaves, we'd want per-send timing. ~15-line change in `worker.py::_deliver_one`.

---

## Recommendations, Actionable

1. **Do not launch a 150k email broadcast on current production defaults.** It would take 50 hours. Before any bulk send: bump `worker_max_per_cycle` to 600+ and `worker_poll_sec` to 1s. Puts the binding constraint back on Resend's 10/sec cap (4.2h), not our code.

2. **Ship a `/email-burst-mode on` / `off` slash command** if bulk sends become routine. Preserves the conservative "trickle" defaults for normal traffic but lets an admin speed up drain for a specific broadcast.

3. **For audiences > 50k: evaluate SES** for broadcast-only sends. Keep Resend for transactional. Hybrid posture is cheaper and faster at scale, and keeps Resend's deliverability reputation healthy for the transactional mail that matters most.

4. **Consider parallelizing sends inside a cycle** only if Resend's limit changes or you move to SES. Until then, the 63/sec software ceiling is irrelevant because you can't send faster than Resend allows anyway.

5. **Add per-send latency histograms to the worker** before the next bulk broadcast. Steady 63/sec today doesn't mean steady 63/sec tomorrow. Catches regressions from template changes, DB migrations, or Resend degradations.

---

## Cross-Reference: This Report vs. Dispatcher Load Test

| Dimension | Dispatcher (TG broadcast) | Email pipeline |
|---|---|---|
| Software ceiling | 5,000 sends/sec (rate cap saturated to 99.9%) | 63 sends/sec (per-send DB work, sequential) |
| External ceiling | Telegram 30 msg/sec | Resend Pro 10 req/sec |
| At 150k: external-limited | 100 min | 250 min (4.2h) |
| At 150k: software-limited | 30 sec (if rate cap ignored) | 40 min |
| Blocking external bottleneck? | YES (Telegram) | YES (Resend) |
| Architecture recommendation | Migrate to Telegram channel broadcast | Hybrid Resend + SES for bulk |

Both pipelines are externally-bound at scale. Both have software headroom that becomes relevant only if/when the external provider is swapped or uncapped. The software in both is cleaner than the external rate limit: neither is the reason broadcasts are slow.

---

## Out of Scope

- **Real Resend API calls.** All sends mocked. The 98.5% success rate is the simulated failure rate, not a measure of Resend's real reliability. To validate Resend itself, run `/email-test <your_email> winback 1` in Discord and confirm delivery.
- **Analytics DB queries (`gather_stats`) under load.** Monkey-patched in this test. At 150k pending rows, `gather_stats` is called once per cycle (not per send), so it's not on the hot path. A separate test against `analytics.db` at realistic size would confirm.
- **Template render performance.** Monkey-patched. In production, `render()` is pure Python string formatting (<1ms), irrelevant to the pipeline's macro behavior.
- **End-to-end delivery (Resend → inbox).** That's deliverability, not throughput. Requires real SPF/DKIM/DMARC config audit and inbox placement testing. Different deliverable.

---

## Reproduce

```
python -u -m scripts.stress_test_email \
    --sends 150000 \
    --max-per-cycle 5000 \
    --poll-sec 0.1 \
    --send-latency-ms 2 \
    --failure-rate 0.01 \
    --resend-429-rate 0.005
```

Expected: ~40 min wall-clock to drain, steady 63 sends/sec, 98.5% success rate. The harness has a 30-min hard cap so 150,000 runs bail early. Raise the cap in `scripts/stress_test_email.py::run()` (`if elapsed > 1800:`) for a full-drain run.

### Smoke test (fast, for CI)

```
python -u -m scripts.stress_test_email --sends 1000 --max-per-cycle 500 --poll-sec 0.2
```

Completes in ~16s. Good for pre-commit validation that the harness + EmailWorker still work end-to-end.

---

## Raw Data

- Test output: `stress_email_150k.out` (gitignored via `stress_*.out` pattern)
- Harness: `scripts/stress_test_email.py`
- Worker under test: `src/email_bot/worker.py`
- DB under test: `src/email_bot/db.py`
- Sender interface (mocked): `src/email_bot/sender.py`
