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
        f"<p style='color:#666;font-size:14px;'>If you need any help feel "
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
        f"<p style='color:#666;font-size:14px;'>If you have any further "
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
    """Day 0: welcome + 3 quickstart steps. Fires on first_seen_at."""
    name = _pretty_name(sub)
    discord = "https://discord.com/channels/1260259552763580537"
    telegram_bot = "https://t.me/PotionScannerBot"

    subject = "Welcome to Potion Alpha"
    text = (
        f"Hey {name},\n\n"
        f"Welcome to Potion. Glad you’re here.\n\n"
        f"Three things to do in the next 5 minutes so you don’t miss "
        f"the next move:\n\n"
        f"1. Open Discord and head to #start-here. Read the pinned post.\n"
        f"2. Set up the Telegram alert bot: {telegram_bot}. /start, then "
        f"/verify. Calls land in your DMs the second they fire.\n"
        f"3. Drop into #questions and say hi. The fastest way to learn "
        f"the room is to ask.\n\n"
        f"Calls fire at all hours. The Telegram bot is what catches them "
        f"when you’re not at the screen. Set it up first.\n\n"
        f"Discord: {discord}\n"
    )
    html_body = (
        f"<p>Hey {escape(name)},</p>"
        f"<p>Welcome to Potion. Glad you’re here.</p>"
        f"<p><strong>Three things to do in the next 5 minutes so you "
        f"don’t miss the next move:</strong></p>"
        f"<ol>"
        f"<li>Open Discord and head to #start-here. Read the pinned post.</li>"
        f"<li>Set up the Telegram alert bot: "
        f"<a href='{escape(telegram_bot)}'>{escape(telegram_bot)}</a>. "
        f"<code>/start</code>, then <code>/verify</code>. Calls land in "
        f"your DMs the second they fire.</li>"
        f"<li>Drop into #questions and say hi. The fastest way to learn "
        f"the room is to ask.</li>"
        f"</ol>"
        f"<p>Calls fire at all hours. The Telegram bot is what catches "
        f"them when you’re not at the screen. Set it up first.</p>"
        f"{_cta_button_html('Open Discord', discord)}"
    )
    return RenderedEmail(subject=subject, text=text, html=_wrap_html(html_body))


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
        f"<p style='color:#666;font-size:14px;'>Manage your Whop: "
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
        f"<p style='color:#666;font-size:14px;'>Already done? Ignore "
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
        f"<p style='color:#666;font-size:14px;'>If you’re leaving "
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
}


def render(
    sequence: str, day: int, subscriber: Subscriber, stats: StatsBundle,
) -> RenderedEmail:
    """Pick the right template for a (sequence, day) pair and render."""
    if sequence == "winback":
        renderer = _WINBACK_RENDERERS.get(day)
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
    return renderer(subscriber, stats)
