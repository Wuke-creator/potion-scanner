"""Two clean attempts to get the Ostium banner rendering INLINE in Gmail.

Both attempts use Resend's documented API surface only — no `inline`
or `disposition` fields (those aren't real Resend fields and were
silently dropped in the previous round of attempts).

Variants:
  A) URL: hosted-image variant. Banner is referenced via a public
     HTTPS URL that Gmail's image proxy can fetch. Requires the
     dashboard's /email-assets/ route to be live (deploy
     dashboard/public/email-assets/ostium-banner.png + the matching
     middleware whitelist).
  B) CID: inline-attachment variant. Banner is base64-encoded into the
     message itself. Resend constructs the multipart/related when both
     `content_id` is set AND the HTML references `cid:<same-id>` with a
     bare ID on both sides.

Run with PREVIEW_TO_EMAIL + RESEND_API_KEY in env. By default both
variants send. Pass --only=A or --only=B to run just one.

First-contact note: Gmail blocks images by default for senders the
recipient hasn't interacted with before. After the first send, click
"Display images below" once. Subsequent sends from the same sender
will render images automatically for that recipient.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TO = os.environ["PREVIEW_TO_EMAIL"]
API_KEY = os.environ["RESEND_API_KEY"]
# Read FROM from the same env var the production bot uses, so we always
# test from the address that's actually going to subscribers. Fall back
# to the canonical Day 0 sender if unset.
FROM = os.environ.get(
    "RESEND_FROM_ADDRESS",
    "Potion Alpha Team <seniormod@mail.potionalpha.com>",
)
BANNER_PATH = ROOT / "static" / "ostium-banner.png"
BANNER_BYTES = BANNER_PATH.read_bytes()
BANNER_B64 = base64.b64encode(BANNER_BYTES).decode("ascii")

# Public URL for the banner. Defaults to the dashboard's /email-assets
# route. Override with BANNER_URL env var if hosting elsewhere.
DEFAULT_BANNER_URL = (
    "https://potion-dashboard-production.up.railway.app"
    "/email-assets/ostium-banner.png"
)
BANNER_URL = os.environ.get("BANNER_URL", DEFAULT_BANNER_URL).strip()

UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def build_html(banner_src: str) -> str:
    """Minimal email body that exercises the banner image only."""
    return f"""\
<!DOCTYPE html>
<html><body style="margin:0;padding:0;background-color:#0a0a0f;
font-family:-apple-system,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0">
<tr><td align="center" style="padding:32px 16px;">
<table width="600" cellpadding="0" cellspacing="0" border="0"
       style="max-width:600px;background:#14141c;border-radius:16px;">
<tr><td style="padding:32px;color:#e8e8ea;font-size:16px;line-height:1.6;">
<p style="margin:0 0 16px 0;color:#fff;font-size:24px;font-weight:700;">
Inline image test
</p>
<p>This email is testing whether the banner below renders <strong>inline
in your Gmail inbox</strong> or as a clickable attachment at the
bottom.</p>
<p style="margin:24px 0 0 0;text-align:center;">
<img src="{banner_src}" width="540" height="180"
     alt="Ostium Gateway to Global Markets"
     style="width:100%;max-width:540px;height:auto;border-radius:12px;
            display:block;margin:0 auto;border:0;" />
</p>
<p style="margin:16px 0 0 0;color:#b0b0b8;font-size:14px;">
If you can see the Ostium banner above, this approach worked.
</p>
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def post_resend(payload: dict) -> str:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": UA_BROWSER,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return f"HTTP {resp.status} {resp.read().decode()}"
    except urllib.error.HTTPError as e:
        return f"HTTPError {e.code} {e.read().decode()}"


# ---------------------------------------------------------------------------
# A) URL: hosted image, Gmail's proxy fetches it
# ---------------------------------------------------------------------------

def attempt_a_url() -> str:
    payload = {
        "from": FROM,
        "to": [TO],
        "subject": "Inline image test A (hosted URL)",
        "html": build_html(BANNER_URL),
        "text": (
            "Inline image test A.\n"
            f"Banner URL: {BANNER_URL}\n"
            "If the URL is reachable, you'll see the banner once Gmail "
            "renders external images for this sender."
        ),
    }
    return f"A (URL): {post_resend(payload)}"


# ---------------------------------------------------------------------------
# B) CID: inline attachment with corrected Resend field shape
# ---------------------------------------------------------------------------

def attempt_b_cid() -> str:
    # Bare ID on both sides. The HTML uses "cid:banner". The attachment
    # carries content_id="banner". No angle brackets, no disposition,
    # no inline boolean — those aren't Resend fields and get dropped.
    payload = {
        "from": FROM,
        "to": [TO],
        "subject": "Inline image test B (CID inline attachment)",
        "html": build_html("cid:banner"),
        "text": "Inline image test B. Open in HTML to see the banner.",
        "attachments": [{
            "filename": "ostium-banner.png",
            "content": BANNER_B64,
            "content_type": "image/png",
            "content_id": "banner",
        }],
    }
    return f"B (CID): {post_resend(payload)}"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--only", choices=["A", "B"], default=None,
                   help="Run only one variant (A=URL, B=CID)")
    args = p.parse_args()

    runs = []
    if args.only in (None, "A"):
        runs.append(("A", attempt_a_url))
    if args.only in (None, "B"):
        runs.append(("B", attempt_b_cid))

    print(f"To: {TO}")
    print(f"From: {FROM}")
    print(f"Banner URL (variant A): {BANNER_URL}")
    print(f"Banner bytes (variant B): {len(BANNER_BYTES):,}")
    print()
    for label, fn in runs:
        try:
            print(fn())
        except Exception as e:
            print(f"{label}: FAILED -> {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
