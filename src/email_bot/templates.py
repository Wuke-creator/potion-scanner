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
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

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


def _cta_button_html(label: str, href: str) -> str:
    safe = escape(href)
    return (
        f'<p style="margin:32px 0;"><a href="{safe}" '
        'style="background:#6b4fbb;color:white;padding:14px 28px;'
        'text-decoration:none;border-radius:8px;font-weight:bold;'
        'display:inline-block;">'
        f'{escape(label)}</a></p>'
    )


def _wrap_html(body: str) -> str:
    """Minimal responsive HTML wrapper."""
    return (
        '<!doctype html><html><body style="font-family:system-ui,sans-serif;'
        'max-width:560px;margin:0 auto;padding:24px;color:#222;'
        'line-height:1.5;">'
        + body
        + '</body></html>'
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
        f"<p style='color:#666;font-size:14px;'>P.S. If something about "
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
        # Offer D (free 3 days)
        subject = f"{name}, a look at the last 30 days"
        top_30 = _top_line_30d(stats) or "strong calls across the board"
        offer_lead = (
            f"Really appreciate your honest opinion. In the background "
            f"we\u2019ve been making improvements.\n\n"
            f"Here\u2019s the peak call from the past 30 days: {top_30}. "
            f"We\u2019d like to give you 3 free days to see if it feels "
            f"different now. No pressure either way."
        )
        cta = "Try 3 Days Free"
    elif reason == "found_alternative":
        # Offer E (compare)
        subject = "A fair comparison"
        top_30 = _top_line_30d(stats) or "our top calls this month"
        offer_lead = (
            f"Respect the honesty. We\u2019re not going to try to outbid "
            f"anyone; instead, here\u2019s a look at our most profitable "
            f"call this last month: {top_30}.\n\n"
            f"No discount, just value. If it doesn\u2019t stack up, we wish "
            f"you well. If you want to try both, there\u2019s a free 3-day trial."
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
    html_body = (
        f"<p>{escape(name)},</p>"
        f"<p>{escape(offer_lead).replace(chr(10) + chr(10), '</p><p>')}</p>"
        f"{_cta_button_html(cta, rejoin)}"
        f"<p style='color:#666;font-size:14px;'>No pressure, just wanted "
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
        f"<p style='color:#666;font-size:14px;'>If you do decide to come "
        f"back later at full price, you\u2019re always welcome. The free "
        f"Discord link is always open: "
        f"<a href='{escape(DISCORD_FREE_INVITE)}'>{escape(DISCORD_FREE_INVITE)}</a>"
        f"</p>"
        f"<p style='color:#666;font-size:14px;'>Either way \u2014 good luck "
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
        f"<p style='color:#666;font-size:14px;'>Tip: Turn on notifications "
        f"for #calls and #alerts so you never miss a setup, even when "
        f"you\u2019re not actively in the room.</p>"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


def _reengage_day3(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
    name = _pretty_name(sub)
    bullets_text = _top_bullets(stats) or "• Several high-conviction setups just this week"
    bullets_html = _top_bullets_html(stats) or (
        "<ul><li>Several high-conviction setups just this week</li></ul>"
    )
    rejoin = sub.rejoin_url or "https://whop.com/potion"

    subject = "You probably missed this"
    text = (
        f"Hey {name},\n\n"
        f"Quick one \u2014 since you\u2019ve been away, here\u2019s a few things you "
        f"missed:\n\n"
        f"WEEKLY RESULTS SNAPSHOT\n"
        f"{bullets_text}\n\n"
        f"Most members don\u2019t even realize how much is inside until they "
        f"start using it properly. Might be worth taking another look.\n\n"
        f"Jump back in: {rejoin}\n\n"
        f"If you need any help feel free to open a ticket and get live "
        f"support from our team: {TICKETS_CHANNEL}\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>Quick one, since you\u2019ve been away, here\u2019s a few things "
        f"you missed:</p>"
        f"<p><strong>Weekly results snapshot:</strong></p>"
        f"{bullets_html}"
        f"<p>Most members don\u2019t even realize how much is inside until "
        f"they start using it properly. Might be worth taking another look.</p>"
        f"{_cta_button_html('Jump back in', rejoin)}"
        f"<p style='color:#666;font-size:14px;'>If you need any help feel "
        f"free to <a href='{escape(TICKETS_CHANNEL)}'>open a ticket</a> and "
        f"get live support from our team.</p>"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


def _reengage_day5(sub: Subscriber, stats: StatsBundle) -> RenderedEmail:
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
    subject = "You missed the last train"
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
        f"<p style='color:#666;font-size:14px;'>If you have any further "
        f"questions, feel free to "
        f"<a href='{escape(TICKETS_CHANNEL)}'>open a ticket</a>.</p>"
        f"<p>Potion Team</p>"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------


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
    1: _reengage_day1,
    3: _reengage_day3,
    5: _reengage_day5,
    7: _reengage_day7,
}


def render(
    sequence: str, day: int, subscriber: Subscriber, stats: StatsBundle,
) -> RenderedEmail:
    """Pick the right template for a (sequence, day) pair and render."""
    if sequence == "winback":
        renderer = _WINBACK_RENDERERS.get(day)
    elif sequence == "reengagement":
        renderer = _REENGAGE_RENDERERS.get(day)
    else:
        raise ValueError(f"unknown sequence: {sequence!r}")
    if renderer is None:
        raise ValueError(f"no template for {sequence!r} day {day}")
    return renderer(subscriber, stats)
