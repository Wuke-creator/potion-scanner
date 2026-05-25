"""Send EVERY template in the email_bot deck to a single recipient.

Sister script to ``preview_send_ab_emails.py``. That one only fires the 11
NEW (behaviour-tree) emails. This one fires all 41: every sequence,
every variant, every save-offer reason. Use when you want a full audit
of how the deck lands in a real inbox.

Usage:
    python tools/send_all_emails.py <to_email>

Env required:
    RESEND_API_KEY            -- send-only restricted key
    RESEND_FROM_ADDRESS       -- like 'Potion Alpha Team <hi@mail.potionalpha.com>'

Each subject is prefixed with [N/41 SECTION:LABEL] so the inbox shows the
deck position. Sends are staggered 2.5s apart so Gmail doesn't flag the
arrival burst as a flood and so we stay well under Resend's per-second cap.

This script bypasses the suppression gate (it talks straight to Resend),
so if the target email has previously bounced or unsubscribed Resend will
reject the send and we'll see it in the per-line FAIL output. That's
intentional: you want to see the raw deck, not a filtered version.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from types import SimpleNamespace

# Load .env so the script works the same way the bot does in dev.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import src.email_bot.templates as T
from src.email_bot.db import Subscriber
from src.email_bot.sender import ResendClient


# ---------------------------------------------------------------------------
# Realistic stub stats matching what gather_stats produces in prod.
# ---------------------------------------------------------------------------


def _make_stats() -> SimpleNamespace:
    return SimpleNamespace(
        calls_7d_total=22,
        wins_7d_over_50pct=5,
        top_call_7d={"pair": "ETH/USDT", "pnl_pct": 89.0, "days_ago": 2},
        top_calls_7d=[
            {"pair": "PEPE/USDT", "pnl_pct": 480.0, "days_ago": 1},
            {"pair": "ETH/USDT", "pnl_pct": 180.0, "days_ago": 2},
            {"pair": "SOL/USDT", "pnl_pct": 120.0, "days_ago": 4},
        ],
        top_pair_7d="ETH/USDT",
        top_pct_7d=89,
        calls_30d_total=92,
        wins_30d_over_50pct=18,
        top_call_30d={"pair": "SOL/USDT", "pnl_pct": 142.0, "days_ago": 18},
        top_calls_30d=[
            {"pair": "SOL/USDT", "pnl_pct": 142.0, "days_ago": 18},
            {"pair": "BONK/USDT", "pnl_pct": 115.0, "days_ago": 25},
            {"pair": "ETH/USDT", "pnl_pct": 89.0, "days_ago": 4},
            {"pair": "PEPE/USDT", "pnl_pct": 67.0, "days_ago": 11},
            {"pair": "ARB/USDT", "pnl_pct": 54.0, "days_ago": 22},
        ],
        top_pair_30d="SOL/USDT",
        top_pnl_pct_30d=142,
    )


def _sub(to_email: str, *, reason: str = "none", trigger: str = "onboarding") -> Subscriber:
    return Subscriber(
        email=to_email,
        name="Luke",
        trigger_type=trigger,
        exit_reason=reason,
        rejoin_url="https://whop.com/potion?promo=BRONZE30-PREVIEW",
        created_at=int(time.time()),
    )


# ---------------------------------------------------------------------------
# Full deck plan -- 41 templates, mirrors tools/dump_all_emails.py
# ---------------------------------------------------------------------------


PLAN = [
    ("WINBACK", [
        ("Day 1", "_winback_day1", dict(reason="too_expensive", trigger="cancellation")),
        ("Day 4", "_winback_day4", dict(reason="too_expensive", trigger="cancellation")),
        ("Day 5 legacy", "_winback_day5_legacy", dict(reason="too_expensive", trigger="cancellation")),
        ("Day 7", "_winback_day7", dict(reason="too_expensive", trigger="cancellation")),
    ]),
    ("REENGAGE", [
        ("Day 1", "_reengage_day1", dict(trigger="inactivity")),
        ("Day 4", "_reengage_day4", dict(trigger="inactivity")),
        ("Day 5 legacy", "_reengage_day5_legacy", dict(trigger="inactivity")),
        ("Day 7", "_reengage_day7", dict(trigger="inactivity")),
    ]),
    ("ONBOARDING", [
        ("Day 0", "_onboard_day0", dict(trigger="onboarding")),
        ("Day 3", "_onboard_day3", dict(trigger="onboarding")),
        ("Day 5", "_onboard_day5", dict(trigger="onboarding")),
        ("Day 7", "_onboard_day7", dict(trigger="onboarding")),
        ("Day 30", "_onboard_day30", dict(trigger="onboarding")),
        ("Monthly", "_onboard_monthly", dict(trigger="onboarding")),
    ]),
    ("DUNNING", [
        ("Day 0", "_dunning_day0", dict(trigger="dunning")),
        ("Day 3", "_dunning_day3", dict(trigger="dunning")),
        ("Day 10", "_dunning_day10", dict(trigger="dunning")),
    ]),
    ("BRONZE", [
        ("Day 0", "_bronze_day0", dict(trigger="bronze")),
        ("Day 1", "_bronze_day1", dict(trigger="bronze")),
        ("Day 3 default", "_bronze_day3", dict(trigger="bronze")),
        ("Day 3 WARM", "_bronze_day3_warm", dict(trigger="bronze")),
        ("Day 3 COLD", "_bronze_day3_cold", dict(trigger="bronze")),
        ("Day 5 default", "_bronze_day5", dict(trigger="bronze")),
        ("Day 5 HOT", "_bronze_day5_hot", dict(trigger="bronze")),
        ("Day 5 WARM", "_bronze_day5_warm", dict(trigger="bronze")),
        ("Day 7", "_bronze_day7", dict(trigger="bronze")),
    ]),
    ("PAID_AT_RISK", [
        ("Day 0", "_paid_at_risk_day0", dict(trigger="paid_at_risk")),
        ("Day 3", "_paid_at_risk_day3", dict(trigger="paid_at_risk")),
    ]),
    ("ONE-SHOTS", [
        ("pre_renewal default", "_pre_renewal", dict(trigger="pre_renewal")),
        ("pre_renewal HIGH", "_pre_renewal_high", dict(trigger="pre_renewal")),
        ("pre_renewal LOW", "_pre_renewal_low", dict(trigger="pre_renewal")),
        ("pre_pause_return", "_pre_pause_return", dict(trigger="pre_pause")),
        ("inactive_day10", "_inactive_day10", dict(trigger="inactive_day10")),
        ("nurture", "_nurture", dict(trigger="nurture")),
        ("post_retention", "_post_retention_day7", dict(trigger="post_retention")),
    ]),
    ("SAVE_OFFER", [
        ("A too_expensive", "_save_offer_day0", dict(reason="too_expensive", trigger="cancellation")),
        ("B not_using", "_save_offer_day0", dict(reason="not_using", trigger="cancellation")),
        ("C market_slow", "_save_offer_day0", dict(reason="market_slow", trigger="cancellation")),
        ("D quality_declined", "_save_offer_day0", dict(reason="quality_declined", trigger="cancellation")),
        ("E found_alternative", "_save_offer_day0", dict(reason="found_alternative", trigger="cancellation")),
        ("F other", "_save_offer_day0", dict(reason="other", trigger="cancellation")),
    ]),
]


async def main() -> int:
    if len(sys.argv) < 2:
        print("usage: send_all_emails.py <to_email>", file=sys.stderr)
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

    stats = _make_stats()

    # Flatten plan into a numbered list for clear progress + subject prefix.
    flat: list[tuple[str, str, str, dict]] = []
    for section, items in PLAN:
        for label, fn_name, kwargs in items:
            flat.append((section, label, fn_name, kwargs))

    total = len(flat)
    print(f"Sending {total} emails to {to_email}")
    print(f"From: {from_address}")
    print(f"Stagger: 2.5s per send (~{total * 2.5 / 60:.1f} minutes total)")
    print("-" * 72)

    results: list[tuple[str, bool, str]] = []
    async with ResendClient(api_key=api_key, from_address=from_address) as client:
        for i, (section, label, fn_name, kwargs) in enumerate(flat, start=1):
            fn = getattr(T, fn_name, None)
            tag_prefix = f"[{i:>2}/{total} {section}:{label}]"
            if fn is None:
                print(f"{tag_prefix} SKIP  renderer {fn_name} not found")
                results.append((f"{section}:{label}", False, "no renderer"))
                continue
            try:
                rendered = fn(_sub(to_email, **kwargs), stats)
            except Exception as e:
                print(f"{tag_prefix} RENDER FAIL  {e!r}")
                results.append((f"{section}:{label}", False, f"render: {e}"))
                continue

            # Prefix the subject so Luke can find each one in the inbox.
            tagged_subject = f"{tag_prefix} {rendered.subject}"
            result = await client.send(
                to=to_email,
                subject=tagged_subject,
                html=rendered.html,
                text=rendered.text,
            )
            ok = result.ok
            note = result.resend_id or result.error or ""
            status = "OK  " if ok else "FAIL"
            print(f"{tag_prefix} {status} {note}")
            results.append((f"{section}:{label}", ok, note))

            if i < total:
                await asyncio.sleep(2.5)

    print("-" * 72)
    ok_count = sum(1 for _, ok, _ in results if ok)
    print(f"Done: {ok_count}/{total} accepted by Resend.")
    if ok_count < total:
        print("Failures:")
        for label, ok, note in results:
            if not ok:
                print(f"  - {label}: {note}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
