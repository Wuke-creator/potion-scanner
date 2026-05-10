"""One-off: render the Day 0 onboarding email locally and POST it to
Resend's /emails endpoint so Luke gets the new dark-card design in his
inbox without waiting on the Railway deploy.

The Ostium banner image is bundled inline as a base64 data URL so it
renders in the recipient's inbox regardless of whether the hosted
/static/ route is live yet.
"""
from __future__ import annotations

import base64
import os
import sys
import urllib.request
import urllib.error
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Set env vars BEFORE importing templates so module-level constants pick up.
os.environ["OSTIUM_TRADE_URL"] = "https://app.ostium.com/?ref=PTION"

# Pre-load the banner bytes so we can attach inline below.
banner_path = ROOT / "static" / "ostium-banner.png"
banner_b64 = base64.b64encode(banner_path.read_bytes()).decode("ascii")

from src.email_bot.db import Subscriber  # noqa: E402
from src.email_bot.stats import StatsBundle  # noqa: E402
from src.email_bot import templates  # noqa: E402

# Banner URL passed via env var. For preview sends we use a Discord CDN
# URL Luke pasted; for production the OSTIUM_BANNER_URL env var on
# Railway will point at /static/ostium-banner.png served by the bot.
banner_url = os.environ.get("OSTIUM_BANNER_URL", "").strip()
if banner_url:
    templates._OSTIUM_BANNER_URL = banner_url


def make_subscriber(email: str, name: str) -> Subscriber:
    return Subscriber(
        email=email, name=name,
        trigger_type="onboarding", exit_reason="none",
        rejoin_url="", created_at=0,
    )


def make_stats() -> StatsBundle:
    return StatsBundle(
        calls_7d_total=12,
        wins_7d_over_50pct=3,
        top_call_7d={"pair": "$WIF", "pnl_pct": 87.0, "days_ago": 1},
        top_calls_7d=[],
        calls_30d_total=45,
        top_call_30d={"pair": "$WIF", "pnl_pct": 120.0, "days_ago": 5},
        top_calls_30d=[],
    )


def main() -> None:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        sys.exit("RESEND_API_KEY env var not set")

    to_addr = os.environ.get("PREVIEW_TO_EMAIL", "").strip()
    if not to_addr:
        sys.exit("PREVIEW_TO_EMAIL env var not set")

    # Force the banner to a CID reference so Gmail can't block it as an
    # external image. The CID is satisfied by an inline attachment below.
    use_cid = os.environ.get("USE_CID_ATTACHMENT", "").strip() == "1"
    if use_cid:
        templates._OSTIUM_BANNER_URL = "cid:ostium-banner-inline"

    rendered = templates._onboard_day0(
        make_subscriber(to_addr, "Luke"), make_stats(),
    )

    payload = {
        "from": "Potion Alpha Team <seniormod@updates.potionalpha.com>",
        "to": [to_addr],
        "subject": rendered.subject + " (preview)",
        "html": rendered.html,
        "text": rendered.text,
    }
    if use_cid:
        payload["attachments"] = [{
            "filename": "ostium-banner.png",
            "content": banner_b64,
            "content_type": "image/png",
            "content_id": "ostium-banner-inline",
            "disposition": "inline",
        }]
    body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Resend sits behind Cloudflare which blocks the default
            # "Python-urllib/X" UA with error 1010. Use a normal browser UA.
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"HTTP {resp.status}")
            print(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        print(f"HTTPError {e.code}")
        print(e.read().decode("utf-8", errors="replace"))
        sys.exit(1)


if __name__ == "__main__":
    main()
