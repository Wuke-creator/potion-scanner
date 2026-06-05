"""Preview-send every NET-NEW behaviour-tree email to a single recipient.

Sends pristine renders (no preview-label pollution) of the 11 new bronze
+ nurture + paid_at_risk + pre_renewal-variant templates so Luke can see
how they actually land in his inbox before we commit + deploy the patch.

Usage:
  python tools/preview_send_ab_emails.py <to_email>

Reads RESEND_API_KEY and RESEND_FROM_ADDRESS from env. The caller is
expected to inject those (e.g. via `railway variables --kv` or by
sourcing a .env). Does not depend on any production DB; uses a stub
Subscriber and a SimpleNamespace stats bundle with realistic numbers.

Staggers 2.5 seconds between sends to stay well under Resend's 2/sec
hard cap and to keep messages in chronological order in the inbox.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass
from types import SimpleNamespace

# We need to import the project modules; the script lives under tools/
# in the repo root, so the import path is already correct when run as
# `python tools/preview_send_ab_emails.py` from the repo root.
import src.email_bot.templates as T
from src.email_bot.db import Subscriber
from src.email_bot.sender import ResendClient


# Realistic stub stats. Real templates do getattr(stats, 'X', default)
# so this SimpleNamespace acts like a stats bundle as far as they care.
def _make_stats() -> SimpleNamespace:
    return SimpleNamespace(
        # 7-day window
        calls_7d_total=22,
        wins_7d_over_50pct=5,
        top_pair_7d="ETH/USDT",
        top_pct_7d=89,
        # 30-day window
        calls_30d_total=92,
        wins_30d_over_50pct=18,
        top_pair_30d="SOL/USDT",
        top_pnl_pct_30d=142,
    )


def _make_subscriber(to_email: str) -> Subscriber:
    return Subscriber(
        email=to_email,
        name="Luke",
        trigger_type="bronze",
        exit_reason="",
        created_at=int(time.time()),
        # Realistic promo-attached link so the BRONZE30 CTAs land
        # against a URL that looks like what a real send produces.
        rejoin_url="https://whop.com/potion?promo=BRONZE30-PREVIEW",
    )


# Render specs: (label, callable-taking-(sub,stats)). Order = chronological
# order Luke would see if he progressed through every branch in turn.
def _build_renders() -> list[tuple[str, callable]]:
    return [
        ("bronze D0 (NEW): welcome",
            lambda s, st: T._bronze_day0(s, st)),
        ("bronze D3 WARM (NEW): transparency",
            lambda s, st: T._bronze_day3_warm(s, st)),
        ("bronze D3 COLD (NEW): re-intro",
            lambda s, st: T._bronze_day3_cold(s, st)),
        ("bronze D5 HOT (NEW): full-urgency offer",
            lambda s, st: T._bronze_day5_hot(s, st)),
        ("bronze D5 WARM (NEW): proof-first",
            lambda s, st: T._bronze_day5_warm(s, st)),
        ("bronze D7 (NEW): last call",
            lambda s, st: T._bronze_day7(s, st)),
        ("nurture (NEW): event-driven big-call gap",
            lambda s, st: T._nurture(s, st)),
        ("paid_at_risk D0 (NEW): catch-before-cancel",
            lambda s, st: T._paid_at_risk_day0(s, st)),
        ("paid_at_risk D3 (NEW): reply-driven check",
            lambda s, st: T._paid_at_risk_day3(s, st)),
        ("pre_renewal HIGH (NEW): frictionless",
            lambda s, st: T._pre_renewal_high(s, st)),
        ("pre_renewal LOW (NEW): justify-the-spend",
            lambda s, st: T._pre_renewal_low(s, st)),
    ]


async def main() -> int:
    if len(sys.argv) < 2:
        print("usage: preview_send_ab_emails.py <to_email>", file=sys.stderr)
        return 2
    to_email = sys.argv[1]
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    from_address = os.environ.get("RESEND_FROM_ADDRESS", "").strip()
    if not api_key or not from_address:
        print(
            "ERROR: RESEND_API_KEY and RESEND_FROM_ADDRESS must be in env.",
            file=sys.stderr,
        )
        return 2

    sub = _make_subscriber(to_email)
    stats = _make_stats()
    renders = _build_renders()

    print(f"Sending {len(renders)} preview emails to {to_email}")
    print(f"From: {from_address}")
    print("-" * 72)

    results: list[tuple[str, str, bool, str]] = []
    async with ResendClient(api_key=api_key, from_address=from_address) as client:
        for i, (label, fn) in enumerate(renders, start=1):
            try:
                rendered = fn(sub, stats)
            except Exception as e:
                print(f"[{i:>2}/{len(renders)}] RENDER FAIL  {label}: {e}")
                results.append((label, "<render error>", False, str(e)))
                continue

            result = await client.send(
                to=to_email,
                subject=rendered.subject,
                html=rendered.html,
                text=rendered.text,
            )
            ok = result.ok
            note = result.resend_id or result.error or ""
            tag = "OK" if ok else "FAIL"
            print(f"[{i:>2}/{len(renders)}] {tag:<4} {label}")
            print(f"        subject: {rendered.subject}")
            print(f"        result:  {note}")
            results.append((label, rendered.subject, ok, note))

            # Stagger to stay under Resend's per-second cap and to keep
            # messages in chronological inbox order.
            if i < len(renders):
                await asyncio.sleep(2.5)

    print("-" * 72)
    ok_count = sum(1 for _, _, ok, _ in results if ok)
    print(f"Done: {ok_count}/{len(results)} delivered to Resend successfully.")
    if ok_count < len(results):
        print("Failures:")
        for label, subject, ok, note in results:
            if not ok:
                print(f"  - {label}: {note}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
