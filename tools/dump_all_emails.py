"""Dump every email template to stdout as plain text.

Sister script to ``preview_send_ab_emails.py``. That one sends the
NEW-only templates to a real inbox via Resend. This one renders ALL
templates (every sequence, every variant) and prints subject + text body
so they can be reviewed in one place without burning Resend quota or
needing the API key in env.

Usage:
    python tools/dump_all_emails.py [output_file]

If no output file given, prints to stdout. HTML is omitted (it's
decorated HTML of the same text).
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from types import SimpleNamespace

import src.email_bot.templates as T
from src.email_bot.db import Subscriber


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


def _sub(*, reason: str = "none", trigger: str = "onboarding") -> Subscriber:
    return Subscriber(
        email="member@example.com",
        name="Luke",
        trigger_type=trigger,
        exit_reason=reason,
        rejoin_url="https://whop.com/potion/recover?code=BRONZE30",
        created_at=int(time.time()),
    )


PLAN = [
    ("WINBACK (cancel)", [
        ("Day 1", "_winback_day1", dict(reason="too_expensive", trigger="cancellation")),
        ("Day 4", "_winback_day4", dict(reason="too_expensive", trigger="cancellation")),
        ("Day 5 (legacy)", "_winback_day5_legacy", dict(reason="too_expensive", trigger="cancellation")),
        ("Day 7", "_winback_day7", dict(reason="too_expensive", trigger="cancellation")),
    ]),
    ("REENGAGEMENT (inactive)", [
        ("Day 1", "_reengage_day1", dict(trigger="inactivity")),
        ("Day 4", "_reengage_day4", dict(trigger="inactivity")),
        ("Day 5 (legacy)", "_reengage_day5_legacy", dict(trigger="inactivity")),
        ("Day 7", "_reengage_day7", dict(trigger="inactivity")),
    ]),
    ("ONBOARDING (new signup)", [
        ("Day 0", "_onboard_day0", dict(trigger="onboarding")),
        ("Day 3", "_onboard_day3", dict(trigger="onboarding")),
        ("Day 5", "_onboard_day5", dict(trigger="onboarding")),
        ("Day 7", "_onboard_day7", dict(trigger="onboarding")),
        ("Day 30", "_onboard_day30", dict(trigger="onboarding")),
        ("Monthly digest", "_onboard_monthly", dict(trigger="onboarding")),
    ]),
    ("DUNNING (failed payment)", [
        ("Day 0", "_dunning_day0", dict(trigger="dunning")),
        ("Day 3", "_dunning_day3", dict(trigger="dunning")),
        ("Day 10", "_dunning_day10", dict(trigger="dunning")),
    ]),
    ("BRONZE (free to Elite)", [
        ("Day 0", "_bronze_day0", dict(trigger="bronze")),
        ("Day 1", "_bronze_day1", dict(trigger="bronze")),
        ("Day 3 default", "_bronze_day3", dict(trigger="bronze")),
        ("Day 3 WARM variant", "_bronze_day3_warm", dict(trigger="bronze")),
        ("Day 3 COLD variant", "_bronze_day3_cold", dict(trigger="bronze")),
        ("Day 5 default", "_bronze_day5", dict(trigger="bronze")),
        ("Day 5 HOT variant", "_bronze_day5_hot", dict(trigger="bronze")),
        ("Day 5 WARM variant", "_bronze_day5_warm", dict(trigger="bronze")),
        ("Day 7", "_bronze_day7", dict(trigger="bronze")),
    ]),
    ("PAID_AT_RISK", [
        ("Day 0", "_paid_at_risk_day0", dict(trigger="paid_at_risk")),
        ("Day 3", "_paid_at_risk_day3", dict(trigger="paid_at_risk")),
    ]),
    ("ONE-SHOTS", [
        ("pre_renewal default", "_pre_renewal", dict(trigger="pre_renewal")),
        ("pre_renewal HIGH variant", "_pre_renewal_high", dict(trigger="pre_renewal")),
        ("pre_renewal LOW variant", "_pre_renewal_low", dict(trigger="pre_renewal")),
        ("pre_pause_return", "_pre_pause_return", dict(trigger="pre_pause")),
        ("inactive_day10", "_inactive_day10", dict(trigger="inactive_day10")),
        ("nurture (big call gap)", "_nurture", dict(trigger="nurture")),
        ("post_retention day 7", "_post_retention_day7", dict(trigger="post_retention")),
    ]),
    ("SAVE OFFER (variants A-F by exit_reason)", [
        ("A: too_expensive", "_save_offer_day0", dict(reason="too_expensive", trigger="cancellation")),
        ("B: not_using", "_save_offer_day0", dict(reason="not_using", trigger="cancellation")),
        ("C: market_slow", "_save_offer_day0", dict(reason="market_slow", trigger="cancellation")),
        ("D: quality_declined", "_save_offer_day0", dict(reason="quality_declined", trigger="cancellation")),
        ("E: found_alternative", "_save_offer_day0", dict(reason="found_alternative", trigger="cancellation")),
        ("F: other", "_save_offer_day0", dict(reason="other", trigger="cancellation")),
    ]),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("output", nargs="?", default=None,
                    help="output file (default stdout)")
    args = ap.parse_args()

    out = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout

    stats = _make_stats()
    total = 0
    failed = 0

    for section, items in PLAN:
        print(file=out)
        print("=" * 78, file=out)
        print(section, file=out)
        print("=" * 78, file=out)
        for label, fn_name, kwargs in items:
            total += 1
            fn = getattr(T, fn_name, None)
            print(file=out)
            print(f"--- {label}  [{fn_name}] ---", file=out)
            if fn is None:
                print(f"[renderer not found in templates.py]", file=out)
                failed += 1
                continue
            try:
                email = fn(_sub(**kwargs), stats)
            except Exception as exc:
                print(f"[render failed: {exc!r}]", file=out)
                failed += 1
                continue
            print(f"SUBJECT: {email.subject}", file=out)
            print(file=out)
            print(email.text.rstrip(), file=out)

    print(file=out)
    print("=" * 78, file=out)
    print(f"Rendered {total - failed}/{total} templates", file=out)
    if failed:
        print(f"({failed} failed)", file=out)

    if args.output:
        out.close()
        print(f"Wrote {total - failed} templates to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
