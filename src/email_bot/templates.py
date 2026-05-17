"""Email templates: 8 sequences + 6 offer variants.

Pure string rendering. No I/O. Given a Subscriber + StatsBundle it returns
``{subject, html, text}`` ready for the sender.

The copy is taken straight from the Drive spec (01_Automated_Email_Sequences
and 06_Offer_Copy). Dynamic values:

  {name}                - subscriber.name, falls back to "there"
  {calls_7d_total}      - int, total signals in last 7 days
  {wins_7d_over_50pct}  - int, calls that hit 50%+
  {top_call_7d_line}    - '+480% on PEPE/USDT' (rendered if data exists)
  {top_calls_7d_bullets}- multi-line bullet list of up to 3 wins
  {rejoin_url}          - per-subscriber rejoin link (from subscriber row)
  {discord_free}        - public Potion Discord invite

UTM tagging:
  After a template returns, render() runs ``_apply_utm`` over the text and
  html bodies. Any URL on a known domain (whop.com, discord.com, discord.gg,
  t.me, potion.*) gets tagged with utm_source=potion_email,
  utm_medium=email, utm_campaign={sequence}_day{day}. This gives the
  analytics layer's ``top_clicked_urls`` per-template granularity even
  though the underlying CTAs share the same destinations across sequences.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
import os
from html import escape

# Ostium plug shown at the bottom of the Day 0 onboarding email so new
# Elite members know perp trading is part of the offering. Both fields
# are env-var driven so the URL or banner can change without a code
# change. ``OSTIUM_BANNER_URL`` is optional — when empty we render the
# section as text + CTA button only (no image). When set, we add a
# linked banner image above the heading.
_OSTIUM_TRADE_URL = os.environ.get(
    "OSTIUM_TRADE_URL", "https://app.ostium.com/?ref=PTION",
)
_OSTIUM_BANNER_URL = os.environ.get("OSTIUM_BANNER_URL", "")

from src.email_bot.db import Subscriber
from src.email_bot.stats import StatsBundle


DISCORD_FREE_INVITE = "https://discord.gg/PotionAlpha"
TOOLS_CHANNEL = "https://discord.com/channels/1260259552763580537/1299761691596161035"
TICKETS_CHANNEL = "https://discord.com/channels/1260259552763580537/1285628366162231346"


@dataclass
class RenderedEmail:
    subject: str
    text: str
    html: str
    from_name: str = "Potion Alpha Team"


# ---------------------------------------------------------------------------
# Placeholder helpers
# ---------------------------------------------------------------------------


def _pretty_name(sub: Subscriber) -> str:
    return (sub.name or "there").strip()


def _top_line(stats: StatsBundle) -> str:
    t = stats.top_call_7d
    if not t:
        return ""
    return f"+{t['pnl_pct']:.0f}% on {t['pair']}"


def _top_line_30d(stats: StatsBundle) -> str:
    t = stats.top_call_30d
    if not t:
        return ""
    return f"+{t['pnl_pct']:.0f}% on {t['pair']}"


def _top_bullets(stats: StatsBundle) -> str:
    """3-line bullet list of top wins, or an empty string if no data."""
    if not stats.top_calls_7d:
        return ""
    lines = []
    for t in stats.top_calls_7d:
        days = t["days_ago"]
        when = "today" if days == 0 else (f"{days}d ago")
        lines.append(f"• +{t['pnl_pct']:.0f}% on {t['pair']} (called {when})")
    return "\n".join(lines)


def _top_bullets_html(stats: StatsBundle) -> str:
    if not stats.top_calls_7d:
        return ""
    items = []
    for t in stats.top_calls_7d:
        days = t["days_ago"]
        when = "today" if days == 0 else f"{days}d ago"
        items.append(
            f"<li>+{t['pnl_pct']:.0f}% on {escape(t['pair'])} "
            f"<em>(called {when})</em></li>"
        )
    return "<ul>" + "".join(items) + "</ul>"


_BRAND_PURPLE = "#6b4fbb"
_BRAND_PURPLE_LIGHT = "#b39ddb"
_BG_PAGE = "#0a0a0f"
_BG_CARD = "#14141c"
_BG_CALLOUT = "#1c1630"
_DIVIDER = "#2a2a3e"
_TEXT_PRIMARY = "#ffffff"
_TEXT_BODY = "#e8e8ea"
_TEXT_SECONDARY = "#c8c8d0"
_TEXT_FOOTER = "#b0b0b8"
_TEXT_FOOTER_DIM = "#808088"
_FONT_STACK = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', "
    "'Oxygen', 'Ubuntu', 'Cantarell', 'Fira Sans', 'Droid Sans', "
    "'Helvetica Neue', sans-serif"
)


def _cta_button_html(label: str, href: str) -> str:
    """Brand-purple pill CTA button matching the Potion 2.0 broadcast.

    Wrapped in a centered <p> so the button always sits on its own line.
    Inline styles only — email clients strip <style> blocks.
    """
    safe_href = escape(href, quote=True)
    safe_label = escape(label)
    return (
        f'<p style="margin:24px 0 8px 0;padding:0;text-align:center;">'
        f'<a href="{safe_href}" target="_blank" rel="noopener noreferrer" '
        f'style="display:inline-block;background-color:{_BRAND_PURPLE};'
        f'color:{_TEXT_PRIMARY};padding:16px 36px;border-radius:8px;'
        f'font-size:16px;font-weight:700;text-decoration:none;'
        f'font-family:{_FONT_STACK};">'
        f'{safe_label} &rarr;</a></p>'
    )


def _wrap_html(
    body: str,
    *,
    eyebrow: str = "POTION ALPHA",
    headline: str = "",
    footer_note: str = "",
) -> str:
    """Wrap email body in the dark-card layout from the Potion 2.0 broadcast.

    Structure (table-based for max email-client compatibility):

      page (bg #0a0a0f, padded)
        card (max 600px, bg #14141c, rounded 16)
          header (eyebrow + optional headline, divider beneath)
          body slot (caller-supplied HTML)
          footer (Potion Alpha Team + Unsubscribe link placeholder)

    Backward-compatible: existing callers that pass only ``body`` get
    a default eyebrow ("POTION ALPHA") and no headline. New callers
    can pass ``headline=...`` to render a big white H1 below the
    eyebrow (matching the broadcast's "The next chapter is here").

    Resend's ``{{{RESEND_UNSUBSCRIBE_URL}}}`` macro is rendered raw so
    the same template ships through both the transactional and broadcast
    paths cleanly.
    """
    eyebrow_html = (
        f'<p style="margin:0;padding:0;color:{_BRAND_PURPLE};font-size:13px;'
        f'font-weight:700;letter-spacing:3px;text-transform:uppercase;">'
        f'{escape(eyebrow)}</p>'
        if eyebrow else ""
    )
    headline_html = (
        f'<h1 style="margin:20px 0 0 0;padding:0;color:{_TEXT_PRIMARY};'
        f'font-size:30px;line-height:1.2;font-weight:700;'
        f'text-align:center;">{escape(headline)}</h1>'
        if headline else ""
    )
    header_section = (
        f'<tr><td align="center" '
        f'style="margin:0;padding:36px 32px 28px 32px;'
        f'border-bottom:1px solid {_DIVIDER};text-align:center;">'
        f'{eyebrow_html}{headline_html}'
        f'</td></tr>'
    ) if (eyebrow_html or headline_html) else ""
    body_section = (
        f'<tr><td '
        f'style="margin:0;padding:24px 32px;color:{_TEXT_BODY};'
        f'font-size:16px;line-height:1.6;font-family:{_FONT_STACK};">'
        f'{body}'
        f'</td></tr>'
    )
    footer_note_html = (
        f'<p style="margin:0 0 12px 0;padding:0;color:{_TEXT_FOOTER};'
        f'font-size:14px;line-height:1.6;">{escape(footer_note)}</p>'
        if footer_note else ""
    )
    footer_section = (
        f'<tr><td align="center" '
        f'style="margin:0;padding:20px 32px;'
        f'border-top:1px solid {_DIVIDER};text-align:center;">'
        f'{footer_note_html}'
        f'<p style="margin:0 0 8px 0;padding:0;color:{_TEXT_FOOTER_DIM};'
        f'font-size:12px;">Potion Alpha Team</p>'
        f'<p style="margin:0;padding:0;color:{_TEXT_FOOTER_DIM};'
        f'font-size:12px;">'
        f'<a href="{{{{{{RESEND_UNSUBSCRIBE_URL}}}}}}" '
        f'style="color:{_TEXT_FOOTER_DIM};text-decoration:underline;" '
        f'target="_blank">Unsubscribe</a> &middot; '
        f'<a href="https://whop.com/potion" '
        f'style="color:{_TEXT_FOOTER_DIM};text-decoration:underline;" '
        f'target="_blank">Visit Potion</a></p>'
        f'</td></tr>'
    )

    return (
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
        '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">'
        '<html dir="ltr" lang="en"><head>'
        '<meta charset="UTF-8" />'
        '<meta name="viewport" content="width=device-width" />'
        '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />'
        '<meta name="x-apple-disable-message-reformatting" />'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge" />'
        '<meta name="format-detection" '
        'content="telephone=no,address=no,email=no,date=no,url=no" />'
        '</head>'
        f'<body style="margin:0;padding:0;background-color:{_BG_PAGE};'
        f'font-family:{_FONT_STACK};">'
        '<table border="0" width="100%" cellpadding="0" cellspacing="0" '
        'role="presentation" align="center">'
        '<tr><td align="center" '
        f'style="background-color:{_BG_PAGE};padding:32px 16px;">'
        '<table border="0" width="600" cellpadding="0" cellspacing="0" '
        f'role="presentation" '
        f'style="max-width:600px;width:100%;'
        f'background-color:{_BG_CARD};border-radius:16px;overflow:hidden;">'
        '<tbody>'
        f'{header_section}{body_section}{footer_section}'
        '</tbody></table>'
        '</td></tr></table>'
        '</body></html>'
    )


def _section_divider_html() -> str:
    """Thin horizontal divider used between sections inside the card body."""
    return (
        f'<div style="margin:24px 0;height:1px;'
        f'background-color:{_DIVIDER};line-height:1px;font-size:1px;">'
        f'<p style="margin:0;padding:0;">&nbsp;</p></div>'
    )


def _purple_accent_p(text_html: str) -> str:
    """A short purple-text paragraph used as a brand subhead."""
    return (
        f'<p style="margin:24px 0 8px 0;padding:0;'
        f'color:{_BRAND_PURPLE_LIGHT};font-size:18px;font-weight:700;'
        f'line-height:1.4;">{text_html}</p>'
    )


def _callout_box_html(eyebrow: str, headline: str, sub: str = "") -> str:
    """Purple-bordered dark-purple card used to highlight a key offer
    (matches the broadcast's '$99/month / 3-day free trial' block)."""
    sub_html = (
        f'<p style="margin:0;padding:0;color:{_TEXT_BODY};font-size:15px;">'
        f'{escape(sub)}</p>'
        if sub else ""
    )
    return (
        f'<table width="100%" border="0" cellpadding="0" cellspacing="0" '
        f'role="presentation" '
        f'style="margin:24px 0;background-color:{_BG_CALLOUT};'
        f'border:1px solid {_BRAND_PURPLE};border-radius:12px;">'
        f'<tr><td align="center" style="padding:24px 20px;text-align:center;">'
        f'<p style="margin:0;padding:0;color:{_BRAND_PURPLE_LIGHT};'
        f'font-size:12px;font-weight:700;letter-spacing:2px;'
        f'text-transform:uppercase;">{escape(eyebrow)}</p>'
        f'<h2 style="margin:12px 0 4px 0;padding:0;color:{_TEXT_PRIMARY};'
        f'font-size:26px;font-weight:700;">{escape(headline)}</h2>'
        f'{sub_html}'
        f'</td></tr></table>'
    )


# ---------------------------------------------------------------------------
# Win-Back sequence (triggered by Whop cancellation)
# ---------------------------------------------------------------------------


def _winback_day1(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """Day 1: soft touch, remind them what they're missing with recent wins."""
    name = _pretty_name(sub)
    wins = stats.wins_7d_over_50pct
    total = stats.calls_7d_total
    alerts_flagged = stats.alerts_7d_flagged if hasattr(stats, "alerts_7d_flagged") else 12
    tools_link = "https://discord.com/channels/1260259552763580537/1299761691596161035"
    rejoin = sub.rejoin_url or "https://whop.com/potion"

    subject = "You could\u2019ve caught these"
    text = (
        f"Hey {name},\n\n"
        f"We noticed you stepped away from Potion recently, so we wanted "
        f"to drop in and show you what\u2019s been going on inside Elite "
        f"since you left:\n\n"
        f"In the last 7 days alone:\n"
        f"\u2022 {wins} calls hit over 50%+ gains\n"
        f"\u2022 The Telegram alert bot flagged {alerts_flagged} high-conviction setups\n"
        f"\u2022 Two new tools dropped in {tools_link} that members are already printing with\n\n"
        f"This isn\u2019t a sales pitch. We just know what it feels like to "
        f"miss plays you could\u2019ve caught.\n\n"
        f"Your Concierge thread is still there. Your setup is still saved. "
        f"If you want to get it back, it takes 30 seconds.\n\n"
        f"Pick up where you left off: {rejoin}\n\n"
        f"P.S. If something about Potion wasn\u2019t working for you, reply "
        f"to this email. We actually take the time to read all the feedback, "
        f"suggestions and thoughts you may have. We are an ever-evolving "
        f"group that is always looking to improve. Help us help you.\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>We noticed you stepped away from Potion recently, so we "
        f"wanted to drop in and show you what\u2019s been going on inside "
        f"Elite since you left:</p>"
        f"<p><strong>In the last 7 days alone:</strong></p>"
        f"<ul>"
        f"<li>{wins} calls hit over 50%+ gains</li>"
        f"<li>The Telegram alert bot flagged {alerts_flagged} high-conviction setups</li>"
        f"<li>Two new tools dropped in "
        f"<a href='{escape(tools_link)}'>#tools-we-use</a> "
        f"that members are already printing with</li>"
        f"</ul>"
        f"<p>This isn\u2019t a sales pitch. We just know what it feels like "
        f"to miss plays you could\u2019ve caught.</p>"
        f"<p>Your Concierge thread is still there. Your setup is still "
        f"saved. If you want to get it back, it takes 30 seconds.</p>"
        f"{_cta_button_html('Pick up where you left off', rejoin)}"
        f"<p style='color:#b0b0b8;font-size:14px;'>P.S. If something about "
        f"Potion wasn\u2019t working for you, reply to this email. We "
        f"actually take the time to read all the feedback, suggestions and "
        f"thoughts you may have. We are an ever-evolving group that is "
        f"always looking to improve. Help us help you.</p>"
    )
    # Keep `total` referenced so linters don't flag; stats accessible if template expanded later.
    _ = total
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


def _winback_day4(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """Day 4: incentive offer, $79/month for 3 months, no-strings urgency."""
    name = _pretty_name(sub)
    rejoin = sub.rejoin_url or "https://whop.com/potion"

    # Luke's exact Day 4 copy, unchanged. Emoji in subject is intentional.
    subject = "Stop being an Outsider, become an Insider \U0001f440"
    text = (
        f"Hey {name},\n\n"
        f"I know we\u2019d both rather you be an Insider than an Outsider. "
        f"So we\u2019re going to do something we don\u2019t normally do. "
        f"$79/month for the next 3 months, 20% off the normal rate, no "
        f"strings. If it\u2019s not clicking, cancel anytime.\n\n"
        f"What you get back immediately:\n"
        f"\u2022 Full Elite access to all channels\n"
        f"\u2022 Telegram alert bot with real-time setups\n"
        f"\u2022 Your personal Concierge thread\n"
        f"\u2022 All tools, guides, and resources\n\n"
        f"This offer expires in 48 hours. Don\u2019t miss out!\n\n"
        f"Rejoin the Cabal: {rejoin}\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>I know we\u2019d both rather you be an Insider than an "
        f"Outsider. So we\u2019re going to do something we don\u2019t "
        f"normally do. <strong>$79/month for the next 3 months, 20% off the "
        f"normal rate, no strings.</strong> If it\u2019s not clicking, "
        f"cancel anytime.</p>"
        f"<p><strong>What you get back immediately:</strong></p>"
        f"<ul>"
        f"<li>Full Elite access to all channels</li>"
        f"<li>Telegram alert bot with real-time setups</li>"
        f"<li>Your personal Concierge thread</li>"
        f"<li>All tools, guides, and resources</li>"
        f"</ul>"
        f"<p style='color:#c23b3b;font-weight:bold;'>"
        f"This offer expires in 48 hours. Don\u2019t miss out!</p>"
        f"{_cta_button_html('Rejoin the Cabal', rejoin)}"
    )
    # Keep stats referenced for future use in template body if desired.
    _ = stats
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


def _winback_day5_legacy(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """DEPRECATED 2026-04-18. Retained only so existing pending day=5 sends
    in the DB can still render if they fire before the scheduler clears them.
    New winback sequences skip day 5 and go straight to day 7.

    Day 5 was segmented by exit_reason (Offer A-F). Luke simplified the
    sequence to 3 emails (days 1/4/7) so this is no longer part of the
    standard cadence, but we keep it here to avoid breaking queued sends.
    """
    name = _pretty_name(sub)
    reason = sub.exit_reason
    rejoin = sub.rejoin_url or "https://whop.com/potion"

    # Default fallback: offers A/B/C/F use the plain-text lead run through
    # `escape` + newline-to-p. Offers D and E override this with a pre-
    # rendered top-5 bullets list by setting offer_lead_html directly.
    offer_lead_html: str | None = None

    # Map reason to (subject, offer_text, offer_html, cta_label)
    if reason == "too_expensive":
        # Offer A
        subject = f"{name}, a cheaper way to stay in"
        offer_lead = (
            "I understand that pricing can be tough. How about this: "
            "$79/month for the next 3 months so you can keep going at a "
            "lower rate.\n\n"
            "Stay for less, don\u2019t miss out on the action. I can apply for "
            "it before the cancellation goes through.\n\n"
            "If this is still too much, we also have an annual option at "
            "$69/mo ($828/year) \u2014 lower monthly cost, one time payment."
        )
        cta = "Stay at $79/month"
    elif reason == "not_using":
        # Offer B (pause)
        subject = f"{name}, pause instead of cancel?"
        offer_lead = (
            "Completely understand if you\u2019re not using it right now. "
            "How about a 30-day pause instead?\n\n"
            "Your spot stays saved, and when you\u2019re ready to jump back in, "
            "everything\u2019s exactly where you left it. Auto-reactivates "
            "\u2014 zero effort."
        )
        cta = "Pause for 30 days"
    elif reason == "market_slow":
        # Offer C (pause)
        subject = f"{name}, pause until things heat up"
        offer_lead = (
            "The market\u2019s been quiet. But sentiment changes quickly in "
            "crypto. We can pause your membership for 30 days and come "
            "back when things heat up. Markets cycle, and when they do, "
            "you\u2019ll want to be in the room."
        )
        cta = "Pause until the market picks up"
    elif reason == "quality_declined":
        # Offer D: top 5 calls + 3 free days. Updated 2026-04-18 to show a
        # list of 5 calls pulled live from analytics rather than just the
        # single top call, matching the refreshed Drive spec Doc 06 Offer D.
        subject = f"{name}, a look at the last 30 days"
        top_bullets_text = _top_calls_30d_bullets_text(stats)
        top_bullets_html = _top_calls_30d_bullets_html(stats)
        offer_lead = (
            f"Appreciate the honest feedback.\n\n"
            f"In the background we\u2019ve been working on improvements. "
            f"Here\u2019s a look at the top 5 calls from the past 30 days:\n\n"
            f"{top_bullets_text}\n\n"
            f"We\u2019d like to give you 3 free days to see if it feels "
            f"different now. No pressure either way."
        )
        # Embed the HTML top-5 list into the offer when we render HTML
        offer_lead_html = (
            f"<p>Appreciate the honest feedback.</p>"
            f"<p>In the background we\u2019ve been working on improvements. "
            f"Here\u2019s a look at the top 5 calls from the past 30 days:</p>"
            f"{top_bullets_html}"
            f"<p>We\u2019d like to give you <strong>3 free days</strong> to "
            f"see if it feels different now. No pressure either way.</p>"
        )
        cta = "Try 3 days free"
    elif reason == "found_alternative":
        # Offer E: no discount, top-5 comparison + free 3-day trial. Updated
        # 2026-04-18 to actually show the top 5 calls instead of just
        # gesturing at them, matching Drive Doc 06 Offer E.
        subject = "A fair comparison"
        top_bullets_text = _top_calls_30d_bullets_text(stats)
        top_bullets_html = _top_calls_30d_bullets_html(stats)
        offer_lead = (
            f"Respect the honesty. We\u2019re not going to try to outbid "
            f"anyone.\n\n"
            f"Instead, here\u2019s a breakdown of our top calls from the last "
            f"30 days so you can compare like for like:\n\n"
            f"{top_bullets_text}\n\n"
            f"No discount. Just the numbers.\n\n"
            f"If it doesn\u2019t stack up, we wish you well. If you want to "
            f"run them side by side, there\u2019s a free 3-day trial on us."
        )
        offer_lead_html = (
            f"<p>Respect the honesty. We\u2019re not going to try to outbid "
            f"anyone.</p>"
            f"<p>Instead, here\u2019s a breakdown of our top calls from the "
            f"last 30 days so you can compare like for like:</p>"
            f"{top_bullets_html}"
            f"<p>No discount. Just the numbers.</p>"
            f"<p>If it doesn\u2019t stack up, we wish you well. If you want "
            f"to run them side by side, there\u2019s a free 3-day trial on us.</p>"
        )
        cta = "Compare and decide"
    else:
        # Offer F (fallback for other / fulfillment / none)
        subject = "We\u2019d like to make it up to you"
        offer_lead = (
            "Thanks for the feedback. We\u2019d love to make it up to you. "
            "Here\u2019s 30% off for 2 months while we work on improvements "
            "based on your input."
        )
        cta = "Claim 30% Off"

    text = (
        f"{name},\n\n"
        f"{offer_lead}\n\n"
        f"{cta}: {rejoin}\n\n"
        f"No pressure, just wanted to be transparent about what\u2019s on the "
        f"table.\n"
    )
    # Offers D and E set offer_lead_html to a pre-rendered block containing
    # the top-5 calls list (with <ul> structure). Other offers use the plain
    # escape + newline-to-paragraph fallback.
    if offer_lead_html is None:
        body_html_content = (
            f"<p>{escape(offer_lead).replace(chr(10) + chr(10), '</p><p>')}</p>"
        )
    else:
        body_html_content = offer_lead_html
    html_body = (
        f"<p>{escape(name)},</p>"
        f"{body_html_content}"
        f"{_cta_button_html(cta, rejoin)}"
        f"<p style='color:#b0b0b8;font-size:14px;'>No pressure, just wanted "
        f"to be transparent about what\u2019s on the table.</p>"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


def _winback_day7(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """Day 7: last chance. Final push at the discount before price reverts."""
    name = _pretty_name(sub)
    rejoin = sub.rejoin_url or "https://whop.com/potion"

    subject = f"Last chance to join the Cabal. {name}"
    text = (
        f"Hey {name},\n\n"
        f"This is the last time we\u2019ll reach out. The Cabal is waiting.\n\n"
        f"Your $79/month offer expires today. After this, it goes back to "
        f"full price and we won\u2019t be sending another discount.\n\n"
        f"No Pressure. If Potion isn\u2019t for you all good. But if it\u2019s "
        f"just the timing or price holding you back, this is your best "
        f"shot.\n\n"
        f"\U0001f449 $79/month. No lock-in. Cancel anytime.\n\n"
        f"One click and you\u2019re back in: {rejoin}\n\n"
        f"If you do decide to come back later at full price, you\u2019re "
        f"always welcome. The free Discord link is always open: "
        f"{DISCORD_FREE_INVITE}\n\n"
        f"Either way \u2014 good luck out there. The markets don\u2019t sleep and "
        f"neither does Potion.\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>This is the last time we\u2019ll reach out. The Cabal is waiting.</p>"
        f"<p>Your <strong>$79/month</strong> offer expires today. After "
        f"this, it goes back to full price and we won\u2019t be sending "
        f"another discount.</p>"
        f"<p>No Pressure. If Potion isn\u2019t for you all good. But if "
        f"it\u2019s just the timing or price holding you back, this is your "
        f"best shot.</p>"
        f"<p><strong>\U0001f449 $79/month. No lock-in. Cancel anytime.</strong></p>"
        f"{_cta_button_html('One click and you\u2019re back in', rejoin)}"
        f"<p style='color:#b0b0b8;font-size:14px;'>If you do decide to come "
        f"back later at full price, you\u2019re always welcome. The free "
        f"Discord link is always open: "
        f"<a href='{escape(DISCORD_FREE_INVITE)}'>{escape(DISCORD_FREE_INVITE)}</a>"
        f"</p>"
        f"<p style='color:#b0b0b8;font-size:14px;'>Either way \u2014 good luck "
        f"out there. The markets don\u2019t sleep and neither does Potion.</p>"
    )
    _ = stats
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


# ---------------------------------------------------------------------------
# Re-engagement sequence (triggered by inactivity detection)
# ---------------------------------------------------------------------------


def _reengage_day1(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    name = _pretty_name(sub)
    rejoin = sub.rejoin_url or "https://whop.com/potion"
    subject = "We miss you in the chat"
    text = (
        f"Hey {name},\n\n"
        f"We noticed you\u2019ve been a bit quiet lately.\n\n"
        f"Things have been moving inside the community and honestly, "
        f"it\u2019s not the same when you\u2019re not here.\n\n"
        f"No pressure at all, just bumping it up in case you got busy.\n\n"
        f"Your spot is still here: {rejoin}\n\n"
        f"Tip: Turn on notifications for #calls and #alerts so you never "
        f"miss a setup, even when you\u2019re not actively in the room.\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>We noticed you\u2019ve been a bit quiet lately.</p>"
        f"<p>Things have been moving inside the community and honestly, "
        f"it\u2019s not the same when you\u2019re not here.</p>"
        f"<p>No pressure at all, just bumping it up in case you got busy.</p>"
        f"{_cta_button_html('Come back to the chat', rejoin)}"
        f"<p style='color:#b0b0b8;font-size:14px;'>Tip: Turn on notifications "
        f"for #calls and #alerts so you never miss a setup, even when "
        f"you\u2019re not actively in the room.</p>"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


def _weekly_results_bullets_text(stats: StatsBundle) -> str:
    """Build a text-mode Weekly Results Snapshot from live analytics.

    Picks up to 3 headline lines: the bot's top "we called" moment (from the
    7-day top call), the best swing-call PnL, and the most notable Telegram
    alert. Everything framed as "we called it", never attributed to a
    specific member (per Luke's 2026-04-18 decision).
    """
    lines: list[str] = []
    if stats.top_call_7d:
        pnl = stats.top_call_7d.get("pnl_pct", 0.0)
        pair = stats.top_call_7d.get("pair", "")
        if pair:
            lines.append(f"\u2022 We called {pair}: +{pnl:.0f}% peak gain")
    if len(stats.top_calls_7d) > 1:
        second = stats.top_calls_7d[1]
        lines.append(
            f"\u2022 {second['pair']} swing call: "
            f"+{second['pnl_pct']:.0f}% in {max(1, second['days_ago'])} day(s)"
        )
    if len(stats.top_calls_7d) > 2:
        third = stats.top_calls_7d[2]
        lines.append(
            f"\u2022 Telegram bot alert on {third['pair']}: "
            f"caught the move early"
        )
    if not lines:
        return "\u2022 Multiple high-conviction setups this week"
    return "\n".join(lines)


def _weekly_results_bullets_html(stats: StatsBundle) -> str:
    """HTML version of the Weekly Results Snapshot, same data as the text
    version wrapped in a <ul>."""
    items: list[str] = []
    if stats.top_call_7d:
        pnl = stats.top_call_7d.get("pnl_pct", 0.0)
        pair = stats.top_call_7d.get("pair", "")
        if pair:
            items.append(
                f"<li>We called <strong>{escape(pair)}</strong>: "
                f"+{pnl:.0f}% peak gain</li>"
            )
    if len(stats.top_calls_7d) > 1:
        second = stats.top_calls_7d[1]
        items.append(
            f"<li><strong>{escape(second['pair'])}</strong> swing call: "
            f"+{second['pnl_pct']:.0f}% in "
            f"{max(1, second['days_ago'])} day(s)</li>"
        )
    if len(stats.top_calls_7d) > 2:
        third = stats.top_calls_7d[2]
        items.append(
            f"<li>Telegram bot alert on "
            f"<strong>{escape(third['pair'])}</strong>: "
            f"caught the move early</li>"
        )
    if not items:
        items.append("<li>Multiple high-conviction setups this week</li>")
    return "<ul>" + "".join(items) + "</ul>"


def _top_calls_30d_bullets_text(stats: StatsBundle) -> str:
    """Text-mode bullet list of the top 5 calls from the last 30 days. Used
    by Offer D + Offer E. Falls back to a generic line if analytics hasn't
    produced enough data yet (new account, etc.)."""
    rows = stats.top_calls_30d or []
    if not rows:
        return "\u2022 Multiple high-conviction calls this month"
    out: list[str] = []
    for call in rows[:5]:
        pair = call.get("pair", "")
        pnl = call.get("pnl_pct", 0.0)
        days = max(1, call.get("days_ago", 0))
        if pair:
            out.append(f"\u2022 {pair}: +{pnl:.0f}% ({days} day(s) ago)")
    return "\n".join(out) or "\u2022 Multiple high-conviction calls this month"


def _top_calls_30d_bullets_html(stats: StatsBundle) -> str:
    rows = stats.top_calls_30d or []
    if not rows:
        return "<ul><li>Multiple high-conviction calls this month</li></ul>"
    items: list[str] = []
    for call in rows[:5]:
        pair = call.get("pair", "")
        pnl = call.get("pnl_pct", 0.0)
        days = max(1, call.get("days_ago", 0))
        if pair:
            items.append(
                f"<li><strong>{escape(pair)}</strong>: +{pnl:.0f}% "
                f"({days} day(s) ago)</li>"
            )
    if not items:
        return "<ul><li>Multiple high-conviction calls this month</li></ul>"
    return "<ul>" + "".join(items) + "</ul>"


def _reengage_day4(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """Day 4 reengagement: "You probably missed this" with Weekly Results
    Snapshot. Replaced the old Day 3 slot in the 2026-04-18 schedule update.

    Per Luke: callouts must be framed as "we called X", not attributed to a
    specific member handle. The snapshot is built from live analytics via
    _weekly_results_bullets_* helpers so it's fresh on every send.
    """
    name = _pretty_name(sub)
    snapshot_text = _weekly_results_bullets_text(stats)
    snapshot_html = _weekly_results_bullets_html(stats)
    rejoin = sub.rejoin_url or "https://whop.com/potion"

    subject = "You probably missed this,"
    text = (
        f"Hey {name},\n\n"
        f"Quick one, since you\u2019ve been away, here\u2019s few things that you "
        f"missed:\n\n"
        f"Weekly Results Snapshot\n"
        f"{snapshot_text}\n\n"
        f"Most members don\u2019t even realize how much is inside until they "
        f"start using it properly. Take another look.\n\n"
        f"If you need any help feel free to open a ticket and get live "
        f"support from our team: {TICKETS_CHANNEL}\n\n"
        f"Reclaim your seat: {rejoin}\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>Quick one, since you\u2019ve been away, here\u2019s few things "
        f"that you missed:</p>"
        f"<p><strong>Weekly Results Snapshot</strong></p>"
        f"{snapshot_html}"
        f"<p>Most members don\u2019t even realize how much is inside until "
        f"they start using it properly. Take another look.</p>"
        f"{_cta_button_html('Reclaim your seat', rejoin)}"
        f"<p style='color:#b0b0b8;font-size:14px;'>If you need any help feel "
        f"free to <a href='{escape(TICKETS_CHANNEL)}'>open a ticket</a> and "
        f"get live support from our team.</p>"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


def _reengage_day5_legacy(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """DEPRECATED 2026-04-18. Reengagement cadence simplified to 1/4/7,
    matching the winback cadence. This renderer stays mapped so in-flight
    day=5 reengagement sends scheduled before the change don't crash on
    delivery."""
    name = _pretty_name(sub)
    rejoin = sub.rejoin_url or "https://whop.com/potion"
    subject = "New features while you were away"
    text = (
        f"Hey {name},\n\n"
        f"While you were away, we added new features that help users get "
        f"back on track:\n\n"
        f"\u2022 Potion Digest \u2014 daily activity and information summaries\n"
        f"\u2022 Updated Guide \u2014 to refresh your memory\n"
        f"\u2022 Perp Bot alerts \u2014 catch calls immediately\n\n"
        f"People are actually getting results just by staying plugged in "
        f"consistently. Just didn\u2019t want you missing out if this is still "
        f"something you care about.\n\n"
        f"Take a look here: {rejoin}\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>While you were away, we added new features that help users "
        f"get back on track:</p>"
        f"<ul>"
        f"<li><strong>Potion Digest</strong> \u2014 daily activity and "
        f"information summaries</li>"
        f"<li><strong>Updated Guide</strong> \u2014 to refresh your memory</li>"
        f"<li><strong>Perp Bot alerts</strong> \u2014 catch calls immediately</li>"
        f"</ul>"
        f"<p>People are actually getting results just by staying plugged "
        f"in consistently. Just didn\u2019t want you missing out if this is "
        f"still something you care about.</p>"
        f"{_cta_button_html('Take a look', rejoin)}"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


def _reengage_day7(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    name = _pretty_name(sub)
    rejoin = sub.rejoin_url or "https://whop.com/potion"
    # Subject updated 2026-04-18 per Drive spec doc 01 Task 6.
    subject = "Don\u2019t miss the last Train bound for Potion Elite"
    text = (
        f"Hey {name},\n\n"
        f"We get it, something comes in the way and you can\u2019t be present "
        f"at the moment.\n\n"
        f"Whatever it is, we\u2019re really looking to see you back with us. "
        f"A lot\u2019s happened. But don\u2019t worry, there\u2019s still time and "
        f"room for you to get back.\n\n"
        f"We can set you up quickly and point you in the right direction "
        f"so you don\u2019t feel lost or behind. Reply to this email and "
        f"we\u2019ll help you directly. No pressure.\n\n"
        f"Come back: {rejoin}\n\n"
        f"If you have any further questions, feel free to open a ticket "
        f"and ask us anything: {TICKETS_CHANNEL}\n\n"
        f"Potion Team\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>We get it, something comes in the way and you can\u2019t be "
        f"present at the moment.</p>"
        f"<p>Whatever it is, we\u2019re really looking to see you back with "
        f"us. A lot\u2019s happened. But don\u2019t worry, there\u2019s still "
        f"time and room for you to get back.</p>"
        f"<p>We can set you up quickly and point you in the right "
        f"direction so you don\u2019t feel lost or behind. Reply to this "
        f"email and we\u2019ll help you directly. No pressure.</p>"
        f"{_cta_button_html('Come back', rejoin)}"
        f"<p style='color:#b0b0b8;font-size:14px;'>If you have any further "
        f"questions, feel free to "
        f"<a href='{escape(TICKETS_CHANNEL)}'>open a ticket</a>.</p>"
        f"<p>Potion Team</p>"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


# ---------------------------------------------------------------------------
# Onboarding sequence (Day 0/3/5/7/30 + monthly digest)
# Triggered when whop_email_sync sees a new member; runs against
# whop_members.first_seen_at offsets.
# ---------------------------------------------------------------------------


def _onboard_day0(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """Day 0: welcome + 3 quickstart steps + Ostium plug. Fires on first_seen_at."""
    name = _pretty_name(sub)
    discord = "https://discord.com/channels/1260259552763580537"
    telegram_bot = "https://t.me/PotionScannerBot"

    subject = "Welcome to Potion Alpha"
    text = (
        f"Hey {name},\n\n"
        f"Welcome to Potion Alpha. Glad you’re here.\n\n"
        f"Three things to do in the next 5 minutes so you don’t miss "
        f"the next move:\n\n"
        f"1. Open Discord and head to #start-here. Read the pinned post.\n"
        f"2. Set up the Telegram alert bot: {telegram_bot}. /start, then "
        f"/verify. Calls land in your DMs the second they fire.\n"
        f"3. Drop into #alpha-chat and say hi. The fastest way to learn "
        f"the room is to ask.\n\n"
        f"Calls fire at all hours. The Telegram bot is what catches them "
        f"when you’re not at the screen. Set it up first.\n\n"
        f"Discord: {discord}\n\n"
        f"Trading perps? Set up your Ostium account here:\n"
        f"{_OSTIUM_TRADE_URL}\n"
    )
    # Ostium plug section (optional banner image + CTA button). Banner
    # only renders when OSTIUM_BANNER_URL is configured; CTA always renders.
    banner_html = ""
    if _OSTIUM_BANNER_URL:
        banner_html = (
            f"<p style='margin:8px 0 0 0;text-align:center;'>"
            f"<a href='{escape(_OSTIUM_TRADE_URL)}' target='_blank' "
            f"rel='noopener noreferrer'>"
            f"<img src='{escape(_OSTIUM_BANNER_URL)}' alt='Ostium' "
            f"style='width:100%;max-width:540px;border-radius:12px;display:block;margin:0 auto;'/>"
            f"</a></p>"
        )
    ostium_section_html = (
        f"{_section_divider_html()}"
        f"{banner_html}"
        f"<p style='margin:16px 0 4px 0;color:{_TEXT_PRIMARY};font-size:18px;font-weight:700;'>"
        f"Trading perps?</p>"
        f"<p style='margin:0 0 12px 0;'>"
        f"Set up your Ostium account and trade onchain perps with one tap "
        f"from any signal we drop:</p>"
        f"{_cta_button_html('Set up Ostium', _OSTIUM_TRADE_URL)}"
    )
    html_body = (
        f"<p style='margin:0 0 16px 0;'>Hey {escape(name)},</p>"
        f"<p style='margin:0 0 16px 0;'>Welcome to Potion Alpha. Glad you’re here.</p>"
        f"<p style='margin:24px 0 12px 0;color:{_TEXT_PRIMARY};font-size:18px;font-weight:700;'>"
        f"Three things to do in the next 5 minutes so you don’t miss the next move:</p>"
        f"<ol style='margin:0 0 16px 0;padding-left:20px;'>"
        f"<li style='margin:0 0 10px 0;'>Open Discord and head to "
        f"<strong>#start-here</strong>. Read the pinned post.</li>"
        f"<li style='margin:0 0 10px 0;'>Set up the Telegram alert bot: "
        f"<a href='{escape(telegram_bot)}' style='color:{_BRAND_PURPLE_LIGHT};'>"
        f"{escape(telegram_bot)}</a>. "
        f"<code style='background:#1f1f2a;color:{_TEXT_BODY};padding:2px 6px;"
        f"border-radius:4px;'>/start</code>, then "
        f"<code style='background:#1f1f2a;color:{_TEXT_BODY};padding:2px 6px;"
        f"border-radius:4px;'>/verify</code>. Calls land in your DMs the "
        f"second they fire.</li>"
        f"<li style='margin:0;'>Drop into <strong>#alpha-chat</strong> and "
        f"say hi. The fastest way to learn the room is to ask.</li>"
        f"</ol>"
        f"<p style='margin:16px 0;'>Calls fire at all hours. The Telegram "
        f"bot is what catches them when you’re not at the screen. Set it "
        f"up first.</p>"
        f"{_cta_button_html('Open Discord', discord)}"
        f"{ostium_section_html}"
    )
    return RenderedEmail(
        subject=subject,
        text=text,
        html=_wrap_html(
            html_body,
            eyebrow="Welcome to Potion Alpha",
            headline="The next move is one tap away.",
        ),
    )


def _onboard_day3(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """Day 3: how to read an alpha call + protect access (backup payment)."""
    name = _pretty_name(sub)
    rejoin = sub.rejoin_url or "https://whop.com/potion"
    discord = "https://discord.com/channels/1260259552763580537"

    subject = "How to read a Potion call"
    text = (
        f"Hey {name},\n\n"
        f"Quick one. Every Potion call has the same shape:\n\n"
        f"• Pair (BTC/USDT, ETH/USDT, etc.)\n"
        f"• Side (LONG / SHORT) and leverage\n"
        f"• Entry price (where to fill)\n"
        f"• Stop loss (your pain threshold)\n"
        f"• TP1, TP2, TP3 (where to take profits, scaled out)\n\n"
        f"The discipline is in the SL and the TPs, not the entry. Most "
        f"members who blow up are the ones who skip the SL.\n\n"
        f"While you’re thinking about it: add a backup payment "
        f"method to your Whop account. We see members lose access over "
        f"a single failed card more than anything else.\n\n"
        f"Manage your Whop: {rejoin}\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>Quick one. Every Potion call has the same shape:</p>"
        f"<ul>"
        f"<li><strong>Pair</strong> (BTC/USDT, ETH/USDT, etc.)</li>"
        f"<li><strong>Side</strong> (LONG / SHORT) and <strong>leverage</strong></li>"
        f"<li><strong>Entry price</strong> (where to fill)</li>"
        f"<li><strong>Stop loss</strong> (your pain threshold)</li>"
        f"<li><strong>TP1, TP2, TP3</strong> (where to take profits, scaled out)</li>"
        f"</ul>"
        f"<p>The discipline is in the SL and the TPs, not the entry. "
        f"Most members who blow up are the ones who skip the SL.</p>"
        f"<p>While you’re thinking about it: <strong>add a backup "
        f"payment method to your Whop account.</strong> We see members "
        f"lose access over a single failed card more than anything else.</p>"
        f"{_cta_button_html('Open Discord', discord)}"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


def _onboard_day5(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """Day 5: real win callout. Pulls top recent call from analytics."""
    name = _pretty_name(sub)
    discord = "https://discord.com/channels/1260259552763580537"

    top_pair = (
        getattr(stats, "top_pair_7d", None) or "ETH/USDT"
    )
    top_pct = getattr(stats, "top_pct_7d", None) or 89

    subject = f"+{top_pct}% on {top_pair}, in case you missed it"
    text = (
        f"Hey {name},\n\n"
        f"This week’s headline call: {top_pair} closed at +{top_pct}%.\n\n"
        f"Not a fluke. The community was in the room when it fired. The "
        f"Telegram bot pinged subscribers within seconds. The members "
        f"who scaled out at TP1 took 30%+ profit and let runners ride.\n\n"
        f"You can see the full play in the calls channel. Every TP hit, "
        f"every breakeven move, every closeout. We don’t hide losers "
        f"either — the track-record channel shows the full history.\n\n"
        f"Open Discord: {discord}\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>This week’s headline call: <strong>{escape(str(top_pair))} "
        f"closed at +{top_pct}%.</strong></p>"
        f"<p>Not a fluke. The community was in the room when it fired. "
        f"The Telegram bot pinged subscribers within seconds. The members "
        f"who scaled out at TP1 took 30%+ profit and let runners ride.</p>"
        f"<p>You can see the full play in the calls channel. Every TP "
        f"hit, every breakeven move, every closeout. We don’t hide "
        f"losers either — the track-record channel shows the full "
        f"history.</p>"
        f"{_cta_button_html('See it in Discord', discord)}"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


def _onboard_day7(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """Day 7: first-week recap + keep-going / trial nudge."""
    name = _pretty_name(sub)
    discord = "https://discord.com/channels/1260259552763580537"
    rejoin = sub.rejoin_url or "https://whop.com/potion"

    calls_7d = getattr(stats, "calls_7d_total", None) or 22
    wins_7d = getattr(stats, "wins_7d_over_50pct", None) or 5

    subject = "Your first week in Potion"
    text = (
        f"Hey {name},\n\n"
        f"Week 1 wrap. Here’s what fired across Potion in the seven "
        f"days you’ve been here:\n\n"
        f"• {calls_7d} structured calls\n"
        f"• {wins_7d} closed at +50% or better\n"
        f"• Daily morning brief, daily VC, weekly Mac sessions\n\n"
        f"If you’ve been listening from the sidelines, this is the "
        f"week to start engaging. Drop into a VC. Reply to a call. Use "
        f"#questions when you’re unsure. The members who participate "
        f"in week 1 are the ones still here at month 6.\n\n"
        f"Open Discord: {discord}\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>Week 1 wrap. Here’s what fired across Potion in the "
        f"seven days you’ve been here:</p>"
        f"<ul>"
        f"<li>{calls_7d} structured calls</li>"
        f"<li>{wins_7d} closed at +50% or better</li>"
        f"<li>Daily morning brief, daily VC, weekly Mac sessions</li>"
        f"</ul>"
        f"<p>If you’ve been listening from the sidelines, "
        f"<strong>this is the week to start engaging.</strong> Drop into "
        f"a VC. Reply to a call. Use #questions when you’re unsure. "
        f"The members who participate in week 1 are the ones still here "
        f"at month 6.</p>"
        f"{_cta_button_html('Open Discord', discord)}"
        f"<p style='color:#b0b0b8;font-size:14px;'>Manage your Whop: "
        f"<a href='{escape(rejoin)}'>{escape(rejoin)}</a></p>"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


def _onboard_day30(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """Day 30: month-in-Potion personal digest + renew/upgrade nudge."""
    name = _pretty_name(sub)
    rejoin = sub.rejoin_url or "https://whop.com/potion"

    calls_30d = getattr(stats, "calls_30d_total", None) or 92
    wins_30d = getattr(stats, "wins_30d_over_50pct", None) or 18
    top_pair = getattr(stats, "top_pair_30d", None) or "ETH/USDT"
    top_pct = getattr(stats, "top_pnl_pct_30d", None) or 142

    subject = "Your month in Potion"
    text = (
        f"Hey {name},\n\n"
        f"Thirty days in. Here’s the recap:\n\n"
        f"• {calls_30d} structured calls\n"
        f"• {wins_30d} closed at +50%+\n"
        f"• Top call: +{top_pct}% on {top_pair}\n\n"
        f"Members who renew at month 1 stick around 6+ months on average. "
        f"The hard part of joining a new community — figuring out the "
        f"format, building habits — is behind you. From here it "
        f"compounds.\n\n"
        f"If you’re thinking about going annual, the math is "
        f"straightforward: 12 months billed annually saves a meaningful "
        f"chunk vs. monthly. The link below shows current pricing.\n\n"
        f"Manage your Whop: {rejoin}\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>Thirty days in. Here’s the recap:</p>"
        f"<ul>"
        f"<li>{calls_30d} structured calls</li>"
        f"<li>{wins_30d} closed at +50%+</li>"
        f"<li>Top call: <strong>+{top_pct}% on {escape(str(top_pair))}</strong></li>"
        f"</ul>"
        f"<p>Members who renew at month 1 stick around 6+ months on "
        f"average. The hard part of joining a new community — "
        f"figuring out the format, building habits — is behind "
        f"you. From here it compounds.</p>"
        f"<p>If you’re thinking about going annual, the math is "
        f"straightforward: 12 months billed annually saves a meaningful "
        f"chunk vs. monthly. The link below shows current pricing.</p>"
        f"{_cta_button_html('Manage your Whop', rejoin)}"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


def _onboard_monthly(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """Monthly digest (Day 60+, recurring): top calls + community pulse.

    Sent on Day 60, 90, 120, ... at 30-day intervals after the Day-30
    onboarding email. Same content as a generic newsletter, personalised
    only by name.
    """
    name = _pretty_name(sub)
    discord = "https://discord.com/channels/1260259552763580537"

    top_pair = getattr(stats, "top_pair_30d", None) or "ETH/USDT"
    top_pct = getattr(stats, "top_pnl_pct_30d", None) or 142
    calls_30d = getattr(stats, "calls_30d_total", None) or 90
    wins_30d = getattr(stats, "wins_30d_over_50pct", None) or 17

    subject = "What Potion caught this month"
    text = (
        f"Hey {name},\n\n"
        f"Last 30 days at a glance:\n\n"
        f"• {calls_30d} structured calls\n"
        f"• {wins_30d} closed at +50%+\n"
        f"• Top call: +{top_pct}% on {top_pair}\n\n"
        f"Drop into Discord if you haven’t in a while. Voice chats "
        f"run daily. Mac’s weekly is on Sunday.\n\n"
        f"Open Discord: {discord}\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>Last 30 days at a glance:</p>"
        f"<ul>"
        f"<li>{calls_30d} structured calls</li>"
        f"<li>{wins_30d} closed at +50%+</li>"
        f"<li>Top call: <strong>+{top_pct}% on {escape(str(top_pair))}</strong></li>"
        f"</ul>"
        f"<p>Drop into Discord if you haven’t in a while. Voice "
        f"chats run daily. Mac’s weekly is on Sunday.</p>"
        f"{_cta_button_html('Open Discord', discord)}"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


# ---------------------------------------------------------------------------
# Dunning sequence (failed payment) — Day 0 / 3 / 10
# Day 7 in the spec is a Discord Concierge ping (not email), out of scope.
# ---------------------------------------------------------------------------


def _dunning_day0(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """Day 0: payment didn’t go through. Heads-up, retry within 3 days."""
    name = _pretty_name(sub)
    rejoin = sub.rejoin_url or "https://whop.com/potion"

    subject = "Your Potion payment didn’t go through"
    text = (
        f"Hey {name},\n\n"
        f"Heads-up: this month’s Potion payment failed to process. "
        f"This is usually one of three things:\n\n"
        f"• Card expired\n"
        f"• Insufficient funds at the moment\n"
        f"• Bank flagged the charge as suspicious\n\n"
        f"Whop will retry the charge automatically over the next 3 days. "
        f"If you want to skip the wait and keep access uninterrupted, "
        f"update your payment method now.\n\n"
        f"Update payment: {rejoin}\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p><strong>Heads-up: this month’s Potion payment failed "
        f"to process.</strong> This is usually one of three things:</p>"
        f"<ul>"
        f"<li>Card expired</li>"
        f"<li>Insufficient funds at the moment</li>"
        f"<li>Bank flagged the charge as suspicious</li>"
        f"</ul>"
        f"<p>Whop will retry the charge automatically over the next 3 "
        f"days. If you want to skip the wait and keep access "
        f"uninterrupted, update your payment method now.</p>"
        f"{_cta_button_html('Update payment', rejoin)}"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


def _dunning_day3(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """Day 3: access will be paused soon. Increase urgency without panic."""
    name = _pretty_name(sub)
    rejoin = sub.rejoin_url or "https://whop.com/potion"

    subject = "Your Potion access will pause in a few days"
    text = (
        f"Hey {name},\n\n"
        f"Quick reminder: your payment from a few days ago is still "
        f"pending. Whop’s been retrying but hasn’t been able to "
        f"complete the charge.\n\n"
        f"If we can’t get a successful charge through in the next "
        f"few days, your Elite role will be removed and you’ll lose "
        f"access to the calls channel and the Telegram alert bot. We "
        f"don’t want that and we don’t think you do either.\n\n"
        f"It takes 60 seconds to update your payment method:\n\n"
        f"Update payment: {rejoin}\n\n"
        f"Already done? Ignore this. The retry will succeed on the next "
        f"attempt.\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>Quick reminder: your payment from a few days ago is still "
        f"pending. Whop’s been retrying but hasn’t been able to "
        f"complete the charge.</p>"
        f"<p><strong>If we can’t get a successful charge through in "
        f"the next few days, your Elite role will be removed</strong> and "
        f"you’ll lose access to the calls channel and the Telegram "
        f"alert bot. We don’t want that and we don’t think you "
        f"do either.</p>"
        f"<p>It takes 60 seconds to update your payment method.</p>"
        f"{_cta_button_html('Update payment', rejoin)}"
        f"<p style='color:#b0b0b8;font-size:14px;'>Already done? Ignore "
        f"this. The retry will succeed on the next attempt.</p>"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


def _dunning_day10(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """Day 10: access paused, reactivation link, last save attempt."""
    name = _pretty_name(sub)
    rejoin = sub.rejoin_url or "https://whop.com/potion"

    subject = "Your Potion access has been paused"
    text = (
        f"Hey {name},\n\n"
        f"After 10 days of failed retries we’ve paused your Potion "
        f"access. Your Elite role has been removed and Telegram alerts "
        f"have stopped.\n\n"
        f"Reactivating takes one click. Update the payment method on "
        f"your Whop account and your Elite role comes back automatically. "
        f"No new signup, no friction — your settings, your "
        f"Concierge thread, everything is still there.\n\n"
        f"Reactivate: {rejoin}\n\n"
        f"If you’re leaving for another reason, reply to this email "
        f"and tell us why. We read every reply.\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p><strong>After 10 days of failed retries we’ve paused "
        f"your Potion access.</strong> Your Elite role has been removed "
        f"and Telegram alerts have stopped.</p>"
        f"<p>Reactivating takes one click. Update the payment method on "
        f"your Whop account and your Elite role comes back automatically. "
        f"No new signup, no friction — your settings, your "
        f"Concierge thread, everything is still there.</p>"
        f"{_cta_button_html('Reactivate', rejoin)}"
        f"<p style='color:#b0b0b8;font-size:14px;'>If you’re leaving "
        f"for another reason, reply to this email and tell us why. We "
        f"read every reply.</p>"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


# ---------------------------------------------------------------------------
# One-shot lifecycle emails
# ---------------------------------------------------------------------------


def _pre_renewal(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """Fired 3 days before billing. 'Here’s what you caught this month.'"""
    name = _pretty_name(sub)
    rejoin = sub.rejoin_url or "https://whop.com/potion"

    calls_30d = getattr(stats, "calls_30d_total", None) or 90
    wins_30d = getattr(stats, "wins_30d_over_50pct", None) or 17
    top_pair = getattr(stats, "top_pair_30d", None) or "ETH/USDT"
    top_pct = getattr(stats, "top_pnl_pct_30d", None) or 142

    subject = "Your Potion renews in 3 days"
    text = (
        f"Hey {name},\n\n"
        f"Quick check-in. Your Elite renews in 3 days. Here’s what "
        f"you got for the last cycle:\n\n"
        f"• {calls_30d} structured calls\n"
        f"• {wins_30d} closed at +50%+\n"
        f"• Top call: +{top_pct}% on {top_pair}\n\n"
        f"Plus the daily VCs, weekly Mac sessions, the Telegram alert "
        f"bot, and the Concierge thread.\n\n"
        f"Nothing to do here — renewal is automatic. This is just "
        f"a heads-up so you know what’s coming and what to expect.\n\n"
        f"Manage your Whop: {rejoin}\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>Quick check-in. Your Elite renews in 3 days. Here’s "
        f"what you got for the last cycle:</p>"
        f"<ul>"
        f"<li>{calls_30d} structured calls</li>"
        f"<li>{wins_30d} closed at +50%+</li>"
        f"<li>Top call: <strong>+{top_pct}% on {escape(str(top_pair))}</strong></li>"
        f"</ul>"
        f"<p>Plus the daily VCs, weekly Mac sessions, the Telegram alert "
        f"bot, and the Concierge thread.</p>"
        f"<p>Nothing to do here — renewal is automatic. This is "
        f"just a heads-up so you know what’s coming and what to "
        f"expect.</p>"
        f"{_cta_button_html('Manage your Whop', rejoin)}"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


def _pre_pause_return(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """Fired 3 days before a paused membership reactivates.

    Pause feature isn’t built yet (Whop config + role flow needed),
    so this template is dormant until that lands. When it lands, the
    cron schedules this 3 days before pause expiry.
    """
    name = _pretty_name(sub)
    rejoin = sub.rejoin_url or "https://whop.com/potion"

    top_pair = getattr(stats, "top_pair_30d", None) or "ETH/USDT"
    top_pct = getattr(stats, "top_pnl_pct_30d", None) or 142

    subject = "Welcome back — here’s what you missed"
    text = (
        f"Hey {name},\n\n"
        f"Your Potion pause ends in 3 days. Elite access comes back "
        f"automatically — no action needed.\n\n"
        f"While you were away the headline call was +{top_pct}% on "
        f"{top_pair}. Plus a stack of smaller wins and a couple of the "
        f"Mac sessions you usually catch.\n\n"
        f"If you want to extend the pause, you can do that from your "
        f"Whop. Otherwise, see you back inside.\n\n"
        f"Manage your Whop: {rejoin}\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>Your Potion pause ends in 3 days. Elite access comes back "
        f"automatically — no action needed.</p>"
        f"<p>While you were away the headline call was "
        f"<strong>+{top_pct}% on {escape(str(top_pair))}</strong>. Plus "
        f"a stack of smaller wins and a couple of the Mac sessions you "
        f"usually catch.</p>"
        f"<p>If you want to extend the pause, you can do that from your "
        f"Whop. Otherwise, see you back inside.</p>"
        f"{_cta_button_html('Manage your Whop', rejoin)}"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


def _inactive_day10(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """10-day inactivity email. Distinct from the 14-day reengagement
    series so we can fire both without double-counting (each tracks its
    own dedupe row in the inactivity DB)."""
    name = _pretty_name(sub)
    discord = "https://discord.com/channels/1260259552763580537"

    top_pair = getattr(stats, "top_pair_7d", None) or "ETH/USDT"
    top_pct = getattr(stats, "top_pct_7d", None) or 89

    subject = "We noticed you’ve been quiet"
    text = (
        f"Hey {name},\n\n"
        f"Haven’t seen you in Discord for 10 days. No pressure — "
        f"life happens. Just dropping in with a quick week-in-review so "
        f"you can catch up:\n\n"
        f"• Headline call: +{top_pct}% on {top_pair}\n"
        f"• The Telegram alert bot has been firing through the week\n"
        f"• Daily morning brief and weekly Mac session both ran on "
        f"schedule\n\n"
        f"If something’s blocking you from engaging — the "
        f"format, missed setups, anything — reply to this email "
        f"and tell us. We read every reply.\n\n"
        f"Open Discord: {discord}\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>Haven’t seen you in Discord for 10 days. No pressure "
        f"— life happens. Just dropping in with a quick week-in-"
        f"review so you can catch up:</p>"
        f"<ul>"
        f"<li>Headline call: <strong>+{top_pct}% on {escape(str(top_pair))}</strong></li>"
        f"<li>The Telegram alert bot has been firing through the week</li>"
        f"<li>Daily morning brief and weekly Mac session both ran on schedule</li>"
        f"</ul>"
        f"<p>If something’s blocking you from engaging — the "
        f"format, missed setups, anything — reply to this email "
        f"and tell us. We read every reply.</p>"
        f"{_cta_button_html('Open Discord', discord)}"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Bronze -> Elite upsell sequence (new Bronze members, days 1 / 3 / 5)
# ---------------------------------------------------------------------------

# The Potion Digest channel. Day 3 points new Bronze members here to see
# a real closed winner they could not act on without Elite.
_DIGEST_CHANNEL = (
    "https://discord.com/channels/1260259552763580537/1491168625472835584"
)

# Canonical Potion support channel (same one the bot's /help and
# /support commands point at). Used for the "need help" P.S.
_SUPPORT_TICKET_CHANNEL = (
    "https://discord.com/channels/1260259552763580537/1285628366162231346"
)


def _bronze_day1(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """Day 1: welcome the new Bronze member, show what Elite unlocks.
    Value-forward, no offer yet."""
    name = _pretty_name(sub)
    rejoin = sub.rejoin_url or "https://whop.com/potion"

    subject = "Everything you’re missing without Elite"
    text = (
        f"Hey {name},\n\n"
        f"Welcome to Potion. You’re in as Bronze, which gets you a seat "
        f"in the room. The plays, the alerts, the edge: those live in "
        f"Elite.\n\n"
        f"Here’s what Bronze does not get you:\n"
        f"• Real-time call alerts the moment a setup fires (Bronze sees "
        f"them late, if at all)\n"
        f"• The Telegram alert bot pinging you the second a trade goes "
        f"live\n"
        f"• Full access to every calls channel and the track record "
        f"behind them\n"
        f"• Your own Concierge thread for direct help\n\n"
        f"Bronze is the lobby. Elite is the floor, and most of the moves "
        f"happen on the floor.\n\n"
        f"See what Elite unlocks: {rejoin}\n\n"
        f"PS. Create a ticket in the Discord if you need any help: "
        f"{_SUPPORT_TICKET_CHANNEL}\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>Welcome to Potion. You’re in as Bronze, which gets you a "
        f"seat in the room. The plays, the alerts, the edge: those live in "
        f"<strong>Elite</strong>.</p>"
        f"<p><strong>Here’s what Bronze does not get you:</strong></p>"
        f"<ul>"
        f"<li>Real-time call alerts the moment a setup fires (Bronze sees "
        f"them late, if at all)</li>"
        f"<li>The Telegram alert bot pinging you the second a trade goes "
        f"live</li>"
        f"<li>Full access to every calls channel and the track record "
        f"behind them</li>"
        f"<li>Your own Concierge thread for direct help</li>"
        f"</ul>"
        f"<p>Bronze is the lobby. Elite is the floor, and most of the moves "
        f"happen on the floor.</p>"
        f"{_cta_button_html('See what Elite unlocks', rejoin)}"
        f"<p style='color:#b0b0b8;font-size:14px;'>PS. Create a ticket in "
        f"the <a href='{_SUPPORT_TICKET_CHANNEL}' "
        f"style='color:#b0b0b8;'>Discord</a> if you need any help.</p>"
    )
    _ = stats
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


def _bronze_day3(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """Day 3: concrete FOMO. A real closed winner from the Potion Digest
    that Bronze could not act on in time."""
    name = _pretty_name(sub)
    rejoin = sub.rejoin_url or "https://whop.com/potion"
    top_pair = getattr(stats, "top_pair_7d", None) or "ETH/USDT"
    top_pct = getattr(stats, "top_pct_7d", None) or 89

    subject = "Bronze watched this one go by"
    text = (
        f"Hey {name},\n\n"
        f"Quick one. +{top_pct}% on {top_pair} closed this week. Elite "
        f"members got the alert in real time and scaled out at TP1. Bronze "
        f"got to read about it after.\n\n"
        f"The full play is sitting in the Potion Digest right now: "
        f"{_DIGEST_CHANNEL}\n\n"
        f"Every entry, every take-profit, every closeout. We don’t hide "
        f"the losers either, the track record shows all of it.\n\n"
        f"That is the real Bronze vs Elite gap. It is not the chat. It is "
        f"the timing.\n\n"
        f"Upgrade to Elite: {rejoin}\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>Quick one. <strong>+{top_pct}% on {escape(str(top_pair))}</strong> "
        f"closed this week. Elite members got the alert in real time and "
        f"scaled out at TP1. Bronze got to read about it after.</p>"
        f"<p>The full play is sitting in the Potion Digest right now:</p>"
        f"{_cta_button_html('See the winner in Potion Digest', _DIGEST_CHANNEL)}"
        f"<p>Every entry, every take-profit, every closeout. We don’t "
        f"hide the losers either, the track record shows all of it.</p>"
        f"<p>That is the real Bronze vs Elite gap. It is not the chat. It "
        f"is the timing.</p>"
        f"{_cta_button_html('Upgrade to Elite', rejoin)}"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


def _bronze_day5(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """Day 5: the offer. 30% off Elite (router-minted personal link via
    sub.rejoin_url) plus a plain how-to-join."""
    name = _pretty_name(sub)
    rejoin = sub.rejoin_url or "https://whop.com/potion"

    subject = "Your 30% off Elite (how to claim it)"
    text = (
        f"Hey {name},\n\n"
        f"You’ve been Bronze for a few days. Here’s a reason to "
        f"move up: 30% off your first stretch of Elite, personal to you.\n\n"
        f"How to claim it:\n"
        f"• Click the link below (your discount is already attached)\n"
        f"• Pick your plan on Whop\n"
        f"• Your Elite role, alert bot, and Concierge thread go live "
        f"within minutes\n\n"
        f"No new signup, no friction. Same account, more access.\n\n"
        f"Claim 30% off Elite: {rejoin}\n\n"
        f"This link is personal to you. Reply if anything is unclear, we "
        f"read every email.\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>You’ve been Bronze for a few days. Here’s a reason to "
        f"move up: <strong>30% off your first stretch of Elite</strong>, "
        f"personal to you.</p>"
        f"<p><strong>How to claim it:</strong></p>"
        f"<ul>"
        f"<li>Click the button below (your discount is already attached)</li>"
        f"<li>Pick your plan on Whop</li>"
        f"<li>Your Elite role, alert bot, and Concierge thread go live "
        f"within minutes</li>"
        f"</ul>"
        f"<p>No new signup, no friction. Same account, more access.</p>"
        f"{_cta_button_html('Claim 30% off Elite', rejoin)}"
        f"<p style='color:#b0b0b8;font-size:14px;'>This link is personal to "
        f"you. Reply if anything is unclear, we read every email.</p>"
    )
    _ = stats
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


_BRONZE_RENDERERS = {
    1: _bronze_day1,
    3: _bronze_day3,
    5: _bronze_day5,
}


_WINBACK_RENDERERS = {
    # Luke's 2026-04-18 simplification: 3 emails at days 1, 4, 7.
    # Day 5 legacy renderer stays mapped so in-flight `day=5` sends from
    # before the change can still render (just won't be scheduled for new
    # cancellations).
    1: _winback_day1,
    4: _winback_day4,
    5: _winback_day5_legacy,
    7: _winback_day7,
}

_REENGAGE_RENDERERS = {
    # Luke's 2026-04-18 simplification: 3 emails at days 1, 4, 7 (same
    # cadence as winback). Day 3 and Day 5 legacy renderers retained so
    # pending sends scheduled before the change don't crash on delivery.
    1: _reengage_day1,
    3: _reengage_day4,  # Day 3 was renamed to Day 4; keep Day 3 key firing
                        # the new renderer so in-flight Day 3 sends still land
    4: _reengage_day4,
    5: _reengage_day5_legacy,
    7: _reengage_day7,
}

_ONBOARDING_RENDERERS = {
    0: _onboard_day0,
    3: _onboard_day3,
    5: _onboard_day5,
    7: _onboard_day7,
    30: _onboard_day30,
    60: _onboard_monthly,
    90: _onboard_monthly,
    120: _onboard_monthly,
    150: _onboard_monthly,
    180: _onboard_monthly,
    # Beyond Day 180 we keep returning the monthly digest. The cron is
    # responsible for not over-scheduling; this mapping just guarantees
    # any (sequence='onboarding', day=N>=60) renders cleanly.
}

_DUNNING_RENDERERS = {
    0: _dunning_day0,
    3: _dunning_day3,
    10: _dunning_day10,
}

_ONESHOT_RENDERERS = {
    "pre_renewal": _pre_renewal,
    "pre_pause_return": _pre_pause_return,
    "inactive_day10": _inactive_day10,
    "save_offer": lambda sub, stats: _save_offer_day0(sub, stats),
    "post_retention": lambda sub, stats: _post_retention_day7(sub, stats),
}


def _post_retention_day7(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """AUT-033 Post-Retention Follow-Up Survey (one-shot, fires 7 days
    after a cancelled member reactivates).

    Drive spec: 05_Survey_Feedback.docx Task 23. The intent is to learn
    WHY the save offer worked — what did the offer surface that the
    user actually wanted? Different from the cancellation survey
    (which asks why they're leaving) — this one asks what convinced
    them to stay so we can lean into it for future at-risk members.

    URL comes through ``sub.rejoin_url``, which the cron sets to the
    configured ``post_retention_survey_url`` env var when scheduling.
    If unset, falls back to a generic CTA pointing at the rejoin URL
    so the email still sends rather than crashing.
    """
    name = _pretty_name(sub)
    survey = sub.rejoin_url or "https://whop.com/potion"

    subject = f"Quick one, {name} — what made you stay?"
    text = (
        f"Hey {name},\n\n"
        f"Glad to have you back. Thanks for giving Potion another shot.\n\n"
        f"One quick favour while it’s fresh: what was the thing that "
        f"actually made you click stay? The offer? The pause option? "
        f"Specific calls you didn’t want to miss? Something else?\n\n"
        f"Two-minute survey, your answers go straight to the team and "
        f"directly shape what we offer next time someone’s on the fence:\n\n"
        f"{survey}\n\n"
        f"No pressure. If you’d rather just reply to this email with "
        f"a one-liner, that works too — we read every response.\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>Glad to have you back. Thanks for giving Potion another shot.</p>"
        f"<p>One quick favour while it’s fresh: <strong>what was the thing "
        f"that actually made you click stay?</strong> The offer? The "
        f"pause option? Specific calls you didn’t want to miss? Something "
        f"else?</p>"
        f"<p>Two-minute survey, your answers go straight to the team and "
        f"directly shape what we offer next time someone’s on the fence:</p>"
        f"{_cta_button_html('Take the 2-minute survey', survey)}"
        f"<p style='color:#b0b0b8;font-size:14px;'>No pressure. If you’d "
        f"rather just reply to this email with a one-liner, that works "
        f"too — we read every response.</p>"
    )
    return RenderedEmail(
        subject=subject, text=text, html=_wrap_html(html_body),
    )


def _save_offer_day0(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    """AUT-026 Targeted Save Offer (one-shot, fires on cancellation).

    Drive spec: 06_Offer_Copy.docx Task 24 (Variants A–F). The router
    (src/automations/save_offer_router.py) maps ``cancel_option`` to one
    of six variants, mints a Whop promo code where applicable, and
    embeds the resulting redemption URL into ``sub.rejoin_url``. This
    template just renders the copy for ``sub.exit_reason``.

    All variants share the same skeleton: greeting, brief acknowledgement
    of the cancel reason, the offer, single CTA pointing at sub.rejoin_url
    (which already carries the promo or pause link). Subject line varies
    per variant so the inbox preview does some of the persuading.

    Unknown reasons fall through to Offer F copy as a defensive default —
    the router doesn't enrol unknown reasons today, but if one slips
    through we'd rather send something coherent than crash.
    """
    name = _pretty_name(sub)
    reason = sub.exit_reason
    rejoin = sub.rejoin_url or "https://whop.com/potion"

    if reason == "too_expensive":
        # Offer A
        subject = f"{name}, a cheaper way to stay in"
        cta = "Stay at $79/month"
        text_body = (
            f"Hey {name},\n\n"
            f"Pricing can be tough, we get it. Before you go, here’s "
            f"something we don’t normally offer: $79/month for the "
            f"next 3 months (20% off our standard rate). No commitment, "
            f"cancel anytime if it’s still not the right fit.\n\n"
            f"Lock it in here: {rejoin}\n\n"
            f"This personal link expires in 14 days. If you’d rather "
            f"go annual, we also offer $69/mo billed yearly ($828) — same "
            f"full Elite access for a lower monthly rate."
        )
        body_html = (
            f"<p>Hey {escape(name)},</p>"
            f"<p>Pricing can be tough, we get it. Before you go, here’s "
            f"something we don’t normally offer: <strong>$79/month for "
            f"the next 3 months (20% off our standard rate)</strong>. No "
            f"commitment, cancel anytime if it’s still not the right fit.</p>"
            f"{_cta_button_html(cta, rejoin)}"
            f"<p style='color:#b0b0b8;font-size:14px;'>This personal link "
            f"expires in 14 days. If you’d rather go annual, we also "
            f"offer $69/mo billed yearly ($828) — same full Elite access "
            f"for a lower monthly rate.</p>"
        )
    elif reason == "not_using":
        # Offer B (pause)
        subject = f"{name}, pause instead of cancel?"
        cta = "Pause for 30 days"
        text_body = (
            f"Hey {name},\n\n"
            f"No point paying if you’re not using it. So how about a "
            f"30-day pause instead of cancelling outright?\n\n"
            f"Your spot stays saved. Telegram bot, Concierge thread, "
            f"channels, all of it. When you’re ready to jump back in, "
            f"everything’s exactly where you left it. Auto-reactivates "
            f"after 30 days unless you extend.\n\n"
            f"Pause now: {rejoin}\n\n"
            f"Zero effort, zero cost, you stay in the network."
        )
        body_html = (
            f"<p>Hey {escape(name)},</p>"
            f"<p>No point paying if you’re not using it. So how about "
            f"a <strong>30-day pause</strong> instead of cancelling outright?</p>"
            f"<p>Your spot stays saved. Telegram bot, Concierge thread, "
            f"channels, all of it. When you’re ready to jump back in, "
            f"everything’s exactly where you left it. Auto-reactivates "
            f"after 30 days unless you extend.</p>"
            f"{_cta_button_html(cta, rejoin)}"
            f"<p style='color:#b0b0b8;font-size:14px;'>Zero effort, zero cost, "
            f"you stay in the network.</p>"
        )
    elif reason == "market_slow":
        # Offer C (pause)
        subject = f"{name}, pause until things heat up"
        cta = "Pause until the market picks up"
        text_body = (
            f"Hey {name},\n\n"
            f"The market’s been quiet, fair call. But sentiment "
            f"changes fast in crypto, and you don’t want to be on the "
            f"sidelines when it does.\n\n"
            f"Instead of cancelling, pause your membership for 30 days. "
            f"Cycle through, see what shifts, come back when the "
            f"environment’s working for you again.\n\n"
            f"Pause now: {rejoin}\n\n"
            f"You won’t be billed during the pause. Auto-reactivates "
            f"on day 31 unless you extend or cancel."
        )
        body_html = (
            f"<p>Hey {escape(name)},</p>"
            f"<p>The market’s been quiet, fair call. But sentiment "
            f"changes fast in crypto, and you don’t want to be on the "
            f"sidelines when it does.</p>"
            f"<p>Instead of cancelling, <strong>pause your membership for "
            f"30 days</strong>. Cycle through, see what shifts, come back "
            f"when the environment’s working for you again.</p>"
            f"{_cta_button_html(cta, rejoin)}"
            f"<p style='color:#b0b0b8;font-size:14px;'>You won’t be billed "
            f"during the pause. Auto-reactivates on day 31 unless you "
            f"extend or cancel.</p>"
        )
    elif reason == "quality_declined":
        # Offer D: 7 free days + top 5 calls digest
        subject = f"{name}, a look at the last 30 days"
        cta = "Try 7 days free"
        bullets_text = _top_calls_30d_bullets_text(stats)
        bullets_html = _top_calls_30d_bullets_html(stats)
        text_body = (
            f"Hey {name},\n\n"
            f"Appreciate you saying so honestly — that kind of feedback is "
            f"how we improve.\n\n"
            f"Quietly behind the scenes we’ve been making changes. "
            f"Here’s the top 5 calls from the last 30 days so you can "
            f"see for yourself:\n\n"
            f"{bullets_text}\n\n"
            f"We’d like to give you 7 free days to see if it feels "
            f"different now. No pressure either way — if it’s still "
            f"not landing for you after the trial, the cancellation "
            f"goes through as planned.\n\n"
            f"Claim 7 days free: {rejoin}"
        )
        body_html = (
            f"<p>Hey {escape(name)},</p>"
            f"<p>Appreciate you saying so honestly — that kind of feedback "
            f"is how we improve.</p>"
            f"<p>Quietly behind the scenes we’ve been making changes. "
            f"Here’s the top 5 calls from the last 30 days so you can "
            f"see for yourself:</p>"
            f"{bullets_html}"
            f"<p><strong>We’d like to give you 7 free days</strong> "
            f"to see if it feels different now. No pressure either way — "
            f"if it’s still not landing for you after the trial, the "
            f"cancellation goes through as planned.</p>"
            f"{_cta_button_html(cta, rejoin)}"
        )
    elif reason == "found_alternative":
        # Offer E: comparison + 7-day trial
        subject = "A fair comparison"
        cta = "Compare and decide"
        bullets_text = _top_calls_30d_bullets_text(stats)
        bullets_html = _top_calls_30d_bullets_html(stats)
        text_body = (
            f"Hey {name},\n\n"
            f"Respect the honesty. We’re not going to try to outbid "
            f"anyone — instead, here’s our last 30 days of calls so "
            f"you can compare like for like:\n\n"
            f"{bullets_text}\n\n"
            f"No discount on this one. Just the numbers.\n\n"
            f"If you want to run them side by side, here’s a 7-day "
            f"free trial — keep both subscriptions, see which one’s "
            f"actually working for you, then decide.\n\n"
            f"Start the comparison: {rejoin}"
        )
        body_html = (
            f"<p>Hey {escape(name)},</p>"
            f"<p>Respect the honesty. We’re not going to try to outbid "
            f"anyone — instead, here’s our last 30 days of calls so "
            f"you can compare like for like:</p>"
            f"{bullets_html}"
            f"<p><strong>No discount on this one. Just the numbers.</strong></p>"
            f"<p>If you want to run them side by side, here’s a 7-day "
            f"free trial — keep both subscriptions, see which one’s "
            f"actually working for you, then decide.</p>"
            f"{_cta_button_html(cta, rejoin)}"
        )
    else:
        # Offer F (other / fulfillment / unknown reason fallback)
        subject = "We’d like to make it up to you"
        cta = "Stay at 25% off"
        text_body = (
            f"Hey {name},\n\n"
            f"Thanks for the feedback. We’d love to make it up to you "
            f"while we work on the things you flagged.\n\n"
            f"Here’s 25% off for 2 months — no strings, just our way "
            f"of saying we hear you.\n\n"
            f"Lock in 25% off: {rejoin}\n\n"
            f"This link is personal to you and expires in 14 days. If "
            f"there’s something specific that drove you to cancel, "
            f"reply to this email — we read every one and we’re "
            f"actively reshaping the room based on member feedback."
        )
        body_html = (
            f"<p>Hey {escape(name)},</p>"
            f"<p>Thanks for the feedback. We’d love to make it up to "
            f"you while we work on the things you flagged.</p>"
            f"<p><strong>Here’s 25% off for 2 months</strong> — no "
            f"strings, just our way of saying we hear you.</p>"
            f"{_cta_button_html(cta, rejoin)}"
            f"<p style='color:#b0b0b8;font-size:14px;'>This link is personal "
            f"to you and expires in 14 days. If there’s something "
            f"specific that drove you to cancel, reply to this email — we "
            f"read every one and we’re actively reshaping the room "
            f"based on member feedback.</p>"
        )

    return RenderedEmail(
        subject=subject, text=text_body, html=_wrap_html(body_html),
    )


def render(
    sequence: str, day: int, subscriber: Subscriber, stats: StatsBundle,
) -> RenderedEmail:
    """Pick the right template for a (sequence, day) pair and render."""
    if sequence == "winback":
        renderer = _WINBACK_RENDERERS.get(day)
    elif sequence == "bronze":
        renderer = _BRONZE_RENDERERS.get(day)
    elif sequence == "reengagement":
        renderer = _REENGAGE_RENDERERS.get(day)
    elif sequence == "onboarding":
        renderer = _ONBOARDING_RENDERERS.get(day)
        # Day > 60 falls back to the monthly digest so a recurring
        # cadence beyond what the table explicitly covers still renders.
        if renderer is None and day >= 60:
            renderer = _onboard_monthly
    elif sequence == "dunning":
        renderer = _DUNNING_RENDERERS.get(day)
    elif sequence in _ONESHOT_RENDERERS:
        # One-shot sequences ignore `day` (always one email per trigger).
        renderer = lambda s, st, _r=_ONESHOT_RENDERERS[sequence]: _r(s, st)  # noqa: E731
    else:
        raise ValueError(f"unknown sequence: {sequence!r}")
    if renderer is None:
        raise ValueError(f"no template for {sequence!r} day {day}")
    rendered = renderer(subscriber, stats)
    return RenderedEmail(
        subject=rendered.subject,
        text=_apply_utm(rendered.text, sequence, day),
        html=_apply_utm(rendered.html, sequence, day),
        from_name=rendered.from_name,
    )


# ---------------------------------------------------------------------------
# UTM tagging
# ---------------------------------------------------------------------------

# Domains we tag. Discord/Telegram don't honor query params but the URL
# still works and the click_url Resend records becomes per-(sequence,day),
# which gives the analytics dashboard per-template top-CTA breakdowns.
_UTM_DOMAINS = (
    "whop.com",
    "discord.com",
    "discord.gg",
    "t.me",
    "potion.wtf",
    "potion.com",
)

# Match an http(s) URL on one of the tagged domains. The trailing char
# class stops at whitespace, HTML attribute terminators, and common text
# punctuation that wouldn't appear inside a URL we authored. Keep this
# intentionally lenient: we'd rather miss a malformed URL than corrupt a
# legitimate one.
_UTM_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:"
    + "|".join(re.escape(d) for d in _UTM_DOMAINS)
    + r")(?:/[^\s<>\"')\]]*)?",
    flags=re.IGNORECASE,
)


def _utm_params(sequence: str, day: int) -> str:
    return (
        f"utm_source=potion_email"
        f"&utm_medium=email"
        f"&utm_campaign={sequence}_day{day}"
    )


def _apply_utm(body: str, sequence: str, day: int) -> str:
    """Append UTM params to every URL in ``body`` whose host is in
    ``_UTM_DOMAINS``. Idempotent: URLs that already have ``utm_source``
    are left alone so a render-after-render-after-render chain never
    duplicates the params.
    """
    if not body:
        return body
    params = _utm_params(sequence, day)

    def _rewrite(match: re.Match) -> str:
        url = match.group(0)
        # Strip trailing punctuation that the regex picked up but a human
        # reader would consider end-of-sentence punctuation (rare but
        # happens in plain-text bodies). We re-append it after tagging.
        trailing = ""
        while url and url[-1] in ".,;:!?":
            trailing = url[-1] + trailing
            url = url[:-1]
        if not url:
            return match.group(0)
        # Skip URLs that already have UTM tracking. Catches double-renders
        # and any links the templates pre-tagged manually.
        lower = url.lower()
        if "utm_source=" in lower:
            return url + trailing
        # Don't touch the path-less host (e.g. "https://discord.gg" with
        # nothing after) — adding query params there would be confusing.
        # The regex requires at least the protocol+host, so an empty path
        # is fine; we still tag it for consistency.
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}{params}{trailing}"

    return _UTM_URL_RE.sub(_rewrite, body)
