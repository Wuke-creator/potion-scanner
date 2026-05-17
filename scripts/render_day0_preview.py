"""One-off: render the Day 0 onboarding email as static HTML files so we
can show what subscribers actually see (e.g. for slide decks / approval
review). Saves two versions:

  data/preview/day0_no_banner.html   - current production state
  data/preview/day0_with_banner.html - with OSTIUM_BANNER_URL set
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# IMPORTANT: env vars must be set BEFORE importing templates so the
# module-level _OSTIUM_* constants pick them up.
os.environ.setdefault("OSTIUM_TRADE_URL", "https://app.ostium.com/?ref=PTION")

from src.email_bot.db import Subscriber  # noqa: E402
from src.email_bot.stats import StatsBundle  # noqa: E402


def make_subscriber() -> Subscriber:
    return Subscriber(
        email="member@example.com",
        name="Alex",
        trigger_type="onboarding",
        exit_reason="none",
        rejoin_url="",
        created_at=0,
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


def render_and_save(banner_url: str, out_path: Path) -> None:
    # Patch the module-level constant before calling. Module is already
    # imported, so we set the attribute directly rather than re-reading env.
    from src.email_bot import templates
    templates._OSTIUM_BANNER_URL = banner_url
    rendered = templates._onboard_day0(make_subscriber(), make_stats())
    # Wrap with a faux email-client chrome (subject + from + recipient)
    # so the file looks like a Gmail / Outlook preview rather than just
    # the body. Match Resend's actual From: + Subject: that the bot uses.
    page = (
        "<!doctype html><html lang='en'><head>"
        "<meta charset='utf-8'/>"
        f"<title>{rendered.subject}</title>"
        "<style>"
        "body{margin:0;background:#f4f3f1;font-family:-apple-system,"
        "BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#222;}"
        ".chrome{background:#fff;max-width:680px;margin:24px auto;"
        "border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,0.08);"
        "overflow:hidden;}"
        ".chrome header{padding:18px 24px;border-bottom:1px solid #eee;"
        "background:#fafafa;}"
        ".chrome header .from{font-size:14px;color:#666;}"
        ".chrome header .from strong{color:#222;}"
        ".chrome header .subject{font-size:18px;font-weight:600;"
        "margin-top:6px;}"
        ".chrome .body{padding:0;}"
        "</style></head><body>"
        "<div class='chrome'>"
        "<header>"
        "<div class='from'>From: <strong>Potion Alpha Team</strong> "
        "&lt;seniormod@mail.potionalpha.com&gt;</div>"
        "<div class='from'>To: member@example.com</div>"
        f"<div class='subject'>{rendered.subject}</div>"
        "</header>"
        "<div class='body'>"
        f"{rendered.html}"
        "</div></div></body></html>"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")


def main() -> None:
    out_dir = ROOT / "data" / "preview"
    render_and_save("", out_dir / "day0_no_banner.html")
    # Use a publicly hosted Ostium screenshot URL as the banner for the
    # "with banner" preview. Replace with the real CDN URL once Luke
    # uploads the banner. Picked a stable Ostium docs image that looks
    # close enough for a deck.
    # Local preview banner (relative URL — works with the local http.server
    # we spin up to view the HTML; production will use the Railway-hosted
    # /static/ostium-banner.png URL set via OSTIUM_BANNER_URL env var).
    banner = "ostium-banner.png"
    render_and_save(banner, out_dir / "day0_with_banner.html")
    print(f"Wrote previews to {out_dir}")


if __name__ == "__main__":
    main()
