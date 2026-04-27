"""Telegram alert formatting.

Pure functions only. No I/O, no async, no Telegram SDK calls. Easy to unit
test against the 28 sample messages in ``signals/samples/``.

Output is HTML-safe for ``parse_mode="HTML"``.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from html import escape
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.parser import ParsedSignal, Side


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _format_price_for_display(price_str: str) -> str:
    """Normalize a price string to plain-decimal form for Telegram display.

    Onsight wallet tracker messages sometimes carry the price in scientific
    notation like '6.5e-7' or '3.94e-06', which is hard to scan in a
    Telegram message. Convert to plain decimal ('0.00000065') with enough
    precision to capture the leading significant digits.

    If the input isn't a parseable number, return as-is (defensive: never
    drop content the parser captured).
    """
    if not price_str:
        return ""
    try:
        eff = float(price_str.replace(",", "").strip())
    except (ValueError, AttributeError):
        return price_str
    if eff <= 0:
        return price_str
    if eff >= 1:
        return f"{eff:,.4f}".rstrip("0").rstrip(".")
    if eff >= 0.0001:
        return f"{eff:.6f}".rstrip("0").rstrip(".")
    return f"{eff:.12f}".rstrip("0").rstrip(".")


# Referral-key rewriting. Onsight-style alert bots often hyperlink tokens /
# tools to trade.padre.gg using a competing affiliate's ref code (e.g.
# ?rk=raybot). When Potion Scanner forwards these to our Telegram audience,
# we rewrite the rk query param to Potion's code so clicks credit Potion
# instead of the competitor.
_PADRE_REF_CODE = os.environ.get("PADRE_REF_CODE", "orangie")
_PADRE_URL_RE = re.compile(
    r"(?P<url>https?://(?:www\.)?trade\.padre\.gg/[^\s<>)\"']+)",
    re.IGNORECASE,
)


def _rewrite_padre_url(url: str, ref_code: str = _PADRE_REF_CODE) -> str:
    """Ensure a trade.padre.gg URL's rk query param equals ``ref_code``.

    Preserves path-style /rk/<code> links as-is (those are already hitting
    a dedicated landing page; rewriting the path would break the redirect).
    Only touches query-param rk on /trade/<chain>/<ca> style URLs.
    """
    try:
        parsed = urlparse(url)
        if "padre.gg" not in (parsed.netloc or "").lower():
            return url
        if parsed.path.lstrip("/").startswith("rk/"):
            return url  # path-style landing page, leave alone
        qs = dict(parse_qsl(parsed.query, keep_blank_values=True))
        qs["rk"] = ref_code
        return urlunparse(parsed._replace(query=urlencode(qs)))
    except Exception:
        return url


def _rewrite_padre_refs_in_text(
    text: str, ref_code: str = _PADRE_REF_CODE,
) -> str:
    """Regex-replace every trade.padre.gg URL in ``text`` with its
    rk-rewritten form. Called on the raw Discord content before the
    markdown converter so rewritten URLs flow into both link hrefs and
    bare-URL positions.
    """
    if not text:
        return ""
    return _PADRE_URL_RE.sub(
        lambda m: _rewrite_padre_url(m.group("url"), ref_code), text,
    )


# Discord markdown → Telegram HTML conversion. Runs on the raw Discord
# message text for freeform-forward channels (memecoin source_type) and
# lifecycle events, so links/bold/italic render cleanly on mobile instead
# of showing as literal asterisks and bracket syntax.

_MD_LINK_ANGLE_RE = re.compile(r"\[([^\]]+)\]\(<([^>]+)>\)")
_MD_LINK_PLAIN_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_MD_UNDERLINE_RE = re.compile(r"__([^_\n]+?)__")
_MD_ITALIC_STAR_RE = re.compile(r"(?<![*\w])\*([^*\n]+?)\*(?!\w)")
_MD_ITALIC_UNDER_RE = re.compile(r"(?<![_\w])_([^_\n]+?)_(?!\w)")
_MD_STRIKE_RE = re.compile(r"~~([^~\n]+?)~~")
_MD_CODE_BLOCK_RE = re.compile(r"```[a-zA-Z]*\n?([\s\S]+?)```", re.MULTILINE)
_MD_CODE_INLINE_RE = re.compile(r"`([^`\n]+?)`")
_BARE_URL_ANGLE_RE = re.compile(r"<(https?://[^>\s]+)>")

# Discord mention syntax. These render as raw <@&id> / <@id> / <#id> blobs
# on Telegram if not stripped — useless to subscribers and visually
# distracting. Match all three (role / user / channel) and remove cleanly.
_DISCORD_ROLE_MENTION_RE = re.compile(r"<@&\d+>")
_DISCORD_USER_MENTION_RE = re.compile(r"<@!?\d+>")
_DISCORD_CHANNEL_MENTION_RE = re.compile(r"<#\d+>")
# Custom emoji <:name:id> / animated <a:name:id> render as raw text on
# Telegram. Replace with the bare emoji name (drops the colons) so at
# least the intent reads correctly.
_DISCORD_CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):\d+>")

# Discord message URLs. We surface these as a clean "View original on
# Discord" link instead of leaking the raw 70-char URL into the body.
_DISCORD_MESSAGE_URL_RE = re.compile(
    r"https?://(?:www\.)?discord\.com/channels/\d+/\d+(?:/\d+)?",
    re.IGNORECASE,
)

_PLACEHOLDER_FMT = "\x00TG_TAG_{i}\x00"


def discord_to_telegram_html(text: str) -> str:
    """Convert Discord markdown into Telegram-HTML-safe output.

    Handles (in order): links with angle-bracketed URLs like
    ``[text](<url>)`` (Discord's no-preview syntax), plain ``[text](url)``
    links, code blocks, inline code, bold ``**x**``, underline ``__x__``,
    italic ``*x*`` / ``_x_``, strikethrough ``~~x~~``, bare angle-bracketed
    URLs like ``<https://...>`` (strip the wrappers).

    Strategy: replace each markdown construct with a unique placeholder
    token holding the final HTML, escape the remaining raw text, then swap
    placeholders back in. Avoids double-escaping the HTML tags we just
    produced.

    Also rewrites trade.padre.gg URLs so the rk query param credits Potion
    (``orangie``) rather than whatever affiliate the source bot embedded.
    """
    if not text:
        return ""

    # Rewrite Padre referral keys BEFORE markdown parsing so the new URL
    # flows into both link hrefs and any bare-text URL positions.
    text = _rewrite_padre_refs_in_text(text)

    # Strip Discord-only syntax that doesn't render on Telegram. Role /
    # user / channel mentions become noise (raw <@&id> blobs); custom
    # emojis become "<:name:id>" gibberish. Cleaning happens before the
    # markdown placeholder pass so the stripped tokens never enter the
    # escape pipeline.
    text = _DISCORD_ROLE_MENTION_RE.sub("", text)
    text = _DISCORD_USER_MENTION_RE.sub("", text)
    text = _DISCORD_CHANNEL_MENTION_RE.sub("", text)
    text = _DISCORD_CUSTOM_EMOJI_RE.sub(r":\1:", text)
    # Collapse any blank lines / leading whitespace the strips left behind
    # so the cleaned message doesn't have ragged gaps where pings used to be.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    tags: list[str] = []

    def _stash(tag_html: str) -> str:
        idx = len(tags)
        tags.append(tag_html)
        return _PLACEHOLDER_FMT.format(i=idx)

    def _link_sub(url: str, label: str) -> str:
        return _stash(
            f'<a href="{escape(url, quote=True)}">{escape(label)}</a>'
        )

    # Order matters: links first (so ** inside link labels gets escaped as
    # label text, not converted), then code blocks (so ** inside code stays
    # literal), then remaining inline constructs.

    def _angle_link(m):
        return _link_sub(m.group(2), m.group(1))
    text = _MD_LINK_ANGLE_RE.sub(_angle_link, text)

    def _plain_link(m):
        return _link_sub(m.group(2), m.group(1))
    text = _MD_LINK_PLAIN_RE.sub(_plain_link, text)

    def _code_block(m):
        return _stash(f"<pre>{escape(m.group(1).rstrip())}</pre>")
    text = _MD_CODE_BLOCK_RE.sub(_code_block, text)

    def _code_inline(m):
        return _stash(f"<code>{escape(m.group(1))}</code>")
    text = _MD_CODE_INLINE_RE.sub(_code_inline, text)

    def _bold(m):
        return _stash(f"<b>{escape(m.group(1))}</b>")
    text = _MD_BOLD_RE.sub(_bold, text)

    def _underline(m):
        return _stash(f"<u>{escape(m.group(1))}</u>")
    text = _MD_UNDERLINE_RE.sub(_underline, text)

    def _italic(m):
        return _stash(f"<i>{escape(m.group(1))}</i>")
    text = _MD_ITALIC_STAR_RE.sub(_italic, text)
    text = _MD_ITALIC_UNDER_RE.sub(_italic, text)

    def _strike(m):
        return _stash(f"<s>{escape(m.group(1))}</s>")
    text = _MD_STRIKE_RE.sub(_strike, text)

    # Bare <https://...> wrappers → strip the <>, leave the URL as-is so
    # Telegram auto-linkifies it.
    text = _BARE_URL_ANGLE_RE.sub(lambda m: m.group(1), text)

    # Bare Discord message URLs → clean "View on Discord" hyperlink. The
    # raw 70-char URL leaks visually and gives the recipient nothing
    # actionable on mobile. The cleaned link still lets desktop users
    # click through to see the original post in context.
    def _discord_url_sub(m):
        return _stash(
            f'<a href="{escape(m.group(0), quote=True)}">View on Discord</a>'
        )
    text = _DISCORD_MESSAGE_URL_RE.sub(_discord_url_sub, text)

    # Escape whatever plain text remains, then re-insert the HTML tags.
    # Replace in REVERSE order: a later-indexed tag may contain a reference
    # to an earlier-indexed tag (e.g. bold around a link) and we need to
    # expand the outer tag first so the inner placeholder is unwrapped
    # before the loop reaches it.
    text = escape(text)
    for i in range(len(tags) - 1, -1, -1):
        text = text.replace(_PLACEHOLDER_FMT.format(i=i), tags[i])
    return text


def format_parsed_signal(
    signal: ParsedSignal,
    ref_link: str,
    channel_name: str,
    source_type_label: str,
    timestamp: str | None = None,
) -> str:
    """Build a Telegram alert from a fully parsed Potion Perps signal."""
    direction = signal.side.value if isinstance(signal.side, Side) else str(signal.side)
    risk = signal.risk_level.value if hasattr(signal.risk_level, "value") else str(signal.risk_level)
    ch = escape(channel_name)

    direction_icon = "\U0001f4c8" if direction == "LONG" else "\U0001f4c9"

    lines = [
        f"\U0001f514 <b>Trade #{signal.trade_id}</b>",
        f"\U0001f4e1 Source: Potion #{ch}",
        "",
        f"\U0001f4b1 Pair: <b>{escape(signal.pair)}</b>",
        f"{direction_icon} Direction: <b>{direction}</b>",
        "",
        f"\u26a0\ufe0f Risk: {risk}",
        f"\U0001f4ca Leverage: <code>{signal.leverage}</code>x",
        "",
        f"\U0001f3af Entry: <code>{signal.entry}</code>",
        f"\U0001f6e1\ufe0f Stop: <code>{signal.stop_loss}</code>",
        "",
        f"\U0001f48e Targets:",
        f"TP1: <code>{signal.tp1}</code>",
        f"TP2: <code>{signal.tp2}</code>",
        f"TP3: <code>{signal.tp3}</code>",
    ]
    return "\n".join(lines)


_OSTIUM_REF_FALLBACK = "PTION"


def _extract_base_symbol(pair: str) -> str:
    """Pull the base ticker out of a free-form pair string.

    Handles "BTC/USDT", "ETH/USD 10x", "SOL", "btc/usdt", etc. Strips
    whitespace, splits on the first slash, and uppercases. Non-alphanumeric
    trailing tokens (like "10x") get dropped so we don't smuggle them into
    the deeplink as part of the symbol.
    """
    if not pair:
        return ""
    head = pair.split("/", 1)[0].strip()
    # Take only the first token (drop trailing "10x", "(LONG)", etc.)
    head = head.split()[0] if head else head
    return re.sub(r"[^A-Za-z0-9]", "", head).upper()


def _build_ostium_trade_url(ref_link: str, pair: str) -> str:
    """Build a per-pair Ostium deeplink that opens the right market directly.

    Ostium's frontend takes ``?from=<BASE>&to=USD&ref=<CODE>`` and routes
    straight into the trade view for that market. Mirrors the per-CA
    deeplink trick we use for Padre/Terminal on memecoin alerts.

    Falls back to the bare ref_link if base extraction fails (defensive:
    never produce a broken Trade button).
    """
    base = _extract_base_symbol(pair)
    if not base:
        return ref_link
    # Preserve whatever ref code is already on the configured URL.
    try:
        parsed = urlparse(ref_link)
        existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
        ref_code = existing.get("ref") or _OSTIUM_REF_FALLBACK
    except Exception:
        ref_code = _OSTIUM_REF_FALLBACK
    qs = urlencode({"from": base, "to": "USD", "ref": ref_code})
    return f"https://app.ostium.com/trade?{qs}"


_BLOFIN_REF_FALLBACK = "potion"


def _build_blofin_trade_url(ref_link: str, pair: str) -> str:
    """Build a per-pair Blofin futures deeplink that opens the trading
    page directly with the referral attribution preserved.

    Blofin's URL shape: ``https://blofin.com/futures/<BASE>-USDT?invitecode=<CODE>``.

    The configured ref_link is typically the partner landing page
    ``https://partner.blofin.com/d/potion`` — we extract the partner
    code from the path's last segment so the deeplink credits the same
    partner as the bare URL would have.

    Falls back to the bare ref_link if base extraction fails.
    """
    base = _extract_base_symbol(pair)
    if not base:
        return ref_link
    # Pull the invite code out of the configured ref URL so we honour
    # whatever code Luke has set in REF_LINK_PERPS. Path-style
    # https://partner.blofin.com/d/<code> stores the code as the last
    # path segment; query-string style ?invitecode=<code> is also
    # supported as a fallback.
    invitecode = _BLOFIN_REF_FALLBACK
    try:
        parsed = urlparse(ref_link)
        if parsed.path:
            segments = [s for s in parsed.path.split("/") if s]
            if segments:
                invitecode = segments[-1] or invitecode
        if parsed.query:
            qs_existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
            invitecode = qs_existing.get("invitecode") or invitecode
    except Exception:
        invitecode = _BLOFIN_REF_FALLBACK
    qs = urlencode({"invitecode": invitecode})
    return f"https://blofin.com/futures/{base}-USDT?{qs}"


def build_signal_keyboard(
    ref_link: str, pair: str,
) -> InlineKeyboardMarkup:
    """Build inline buttons for a parsed signal alert.

    Row 1: "Trade now" (ref link) + "Chart" (DexScreener search)

    For Ostium ref links we rewrite the Trade-now URL to a per-pair deeplink
    so the right market opens with one tap (matching the Padre/Terminal
    per-CA deeplink behaviour on memecoin alerts).
    """
    # Build a DexScreener search URL from the base token (first in the pair)
    base_token = pair.split("/")[0].strip() if "/" in pair else pair.strip()
    chart_url = f"https://dexscreener.com/search?q={base_token}"

    trade_url = _resolve_trade_url(ref_link, pair)

    buttons = [
        [
            InlineKeyboardButton(text="\U0001f7e2 Trade now", url=trade_url),
            InlineKeyboardButton(text="\U0001f4ca Chart", url=chart_url),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def _resolve_trade_url(ref_link: str, pair: str) -> str:
    """Return the right Trade-now URL for a (ref_link, pair) combination.

    Per-pair deeplink for Ostium and Blofin (the two perp exchanges we
    route to today). Anything else falls through to the bare ref link
    unchanged. Centralised here so the new-signal keyboard, lifecycle
    Trade-Now link, and any future Trade-now contexts all share one
    consistent URL builder.
    """
    if not ref_link:
        return ref_link
    lower = ref_link.lower()
    if "app.ostium.com" in lower:
        return _build_ostium_trade_url(ref_link, pair)
    if "blofin.com" in lower:
        return _build_blofin_trade_url(ref_link, pair)
    return ref_link


def format_lifecycle_event(
    label: str,
    raw_message: str,
    ref_link: str,
    channel_name: str,
    source_type_label: str,
    timestamp: str | None = None,
    original_signal=None,
) -> str:
    """Format a lifecycle update (TP hit, breakeven, stop hit, etc.).

    When ``original_signal`` (an OpenSignal from open_signals_db) is
    provided, append a "From the original call" block with the entry, SL,
    and TPs that the update message itself doesn't carry. This is what
    makes image-bot updates ("WET Update: TP1 here") actually useful on
    Telegram: the recipient sees the actual TP1 price the user is being
    told to take profit at.

    The "Trade Now" link gets per-pair Ostium deeplink treatment when
    the channel's ref_link points at app.ostium.com AND we have a pair
    (from original_signal or extracted from the raw caption). Mirrors
    the deeplink behaviour of the new-signal Trade-now button so a TP1
    update for WET opens the WET market directly with one tap.
    """
    cleaned = discord_to_telegram_html(raw_message.strip())
    if len(cleaned) > 1500:
        cleaned = cleaned[:1500] + "..."
    ch = escape(channel_name)

    lines = [
        f"<b>Trade Update: {escape(label)}</b>",
        f"<i>Source: Potion #{ch}</i>",
        "",
        cleaned,
    ]

    # Original-signal context block (only when the memory layer found a
    # match in open_signals).
    pair_for_link = ""
    if original_signal is not None:
        pair_for_link = getattr(original_signal, "pair", "") or ""
        ctx = _format_original_signal_block(original_signal)
        if ctx:
            lines.append("")
            lines.append(ctx)

    # Build the Trade-Now URL. Per-pair deeplink (Ostium or Blofin) when
    # the ref link supports one AND we have a pair. Otherwise use the
    # channel's bare ref link unchanged. Pair sourced from the original
    # signal first, then a regex extract from the caption (handles
    # "WET Update: TP1 here" posts that didn't hit the memory layer).
    pair_candidate = pair_for_link or _extract_pair_from_caption(raw_message)
    trade_url = _resolve_trade_url(ref_link, pair_candidate)

    lines.append("")
    lines.append(f'Trade Now: <a href="{escape(trade_url)}">here</a>')
    lines.append("")
    lines.append(f"<i>{timestamp or _utc_now()}</i>")
    return "\n".join(lines)


# Pair-extraction regex for lifecycle captions. Pulls a 2-10 char ticker
# that sits next to a lifecycle keyword (Update / TP\d / SL / Hit / etc.).
# Permissive on purpose: a false hit just means we OCR'd a non-ticker and
# the Ostium deeplink falls back to the bare ref URL when the URL builder
# can't find a sensible base.
_LIFECYCLE_TICKER_RE = re.compile(
    r"\b([A-Z][A-Z0-9]{1,9})\b(?=\s*(?:UPDATE|UPDATES|TP\d|SL|STOP|HIT|MOVE|BE|BREAKEVEN))",
    re.IGNORECASE,
)


def _extract_pair_from_caption(text: str) -> str:
    """Best-effort ticker extraction from a lifecycle caption.

    Returns an empty string when no candidate is found. Returns just the
    base ticker (uppercased), not a full pair — the Ostium deeplink
    builder takes either form.
    """
    if not text:
        return ""
    # Try "PAIR: WET/USDT" structured form first.
    m = re.search(r"PAIR\s*[:#]?\s*(\S+/\S+)", text, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    m = _LIFECYCLE_TICKER_RE.search(text)
    return m.group(1).upper() if m else ""


def _format_original_signal_block(sig) -> str:
    """Render an OpenSignal as an "original call" block in the same visual
    style as ``format_parsed_signal``: emoji bullets per row + ``<code>``
    tags around every numeric value so users can tap-and-hold to copy the
    exact entry / SL / TP price.

    Lays out only the fields that are populated — image-OCR-extracted
    rows often miss the SL or one of the TPs, and we'd rather show a
    short clean block than print 'None' placeholders.
    """
    pair = getattr(sig, "pair", None) or ""
    side = getattr(sig, "side", None) or ""
    leverage = getattr(sig, "leverage", None)
    entry = getattr(sig, "entry", None)
    sl = getattr(sig, "stop_loss", None)
    tp1 = getattr(sig, "tp1", None)
    tp2 = getattr(sig, "tp2", None)
    tp3 = getattr(sig, "tp3", None)

    if not any([pair, side, entry, sl, tp1, tp2, tp3]):
        return ""

    direction_icon = (
        "\U0001f4c8" if str(side).upper() == "LONG"
        else "\U0001f4c9" if str(side).upper() == "SHORT"
        else "\U0001f4ca"
    )

    parts: list[str] = ["<b>From the original call:</b>"]
    if pair:
        parts.append(f"\U0001f4b1 Pair: <b>{escape(str(pair))}</b>")
    if side:
        parts.append(f"{direction_icon} Direction: <b>{escape(str(side))}</b>")
    if leverage:
        parts.append(f"\U0001f4ca Leverage: <code>{int(leverage)}</code>x")
    # Insert blank-row separator if we just rendered the head block AND
    # have at least one numeric field below.
    head_rendered = any([pair, side, leverage])
    body_rendered = any([entry is not None, sl is not None,
                         tp1 is not None, tp2 is not None, tp3 is not None])
    if head_rendered and body_rendered:
        parts.append("")
    if entry is not None:
        parts.append(
            f"\U0001f3af Entry: <code>{_format_price_for_display(str(entry))}</code>"
        )
    if sl is not None:
        parts.append(
            f"\U0001f6e1️ Stop: <code>{_format_price_for_display(str(sl))}</code>"
        )
    has_tps = any(tp is not None for tp in (tp1, tp2, tp3))
    if has_tps:
        # Blank line before the targets section to mirror format_parsed_signal.
        if entry is not None or sl is not None:
            parts.append("")
        parts.append("\U0001f48e Targets:")
        if tp1 is not None:
            parts.append(
                f"TP1: <code>{_format_price_for_display(str(tp1))}</code>"
            )
        if tp2 is not None:
            parts.append(
                f"TP2: <code>{_format_price_for_display(str(tp2))}</code>"
            )
        if tp3 is not None:
            parts.append(
                f"TP3: <code>{_format_price_for_display(str(tp3))}</code>"
            )
    return "\n".join(parts)


def format_unknown_message(
    raw_message: str,
    ref_link: str,
    channel_name: str,
    source_type_label: str,
    timestamp: str | None = None,
) -> str:
    """Forward an unparseable but non-noise message verbatim.

    Runs Discord markdown through ``discord_to_telegram_html`` so links,
    bold, and italic render correctly on Telegram instead of leaking as
    literal ``**``/``[…](…)``/``&lt;…&gt;`` tokens.
    """
    cleaned = discord_to_telegram_html(raw_message.strip())
    if len(cleaned) > 1500:
        cleaned = cleaned[:1500] + "..."
    ch = escape(channel_name)

    lines = [
        f"<b>New Call Detected</b>",
        f"<i>Source: Potion #{ch}</i>",
        "",
        cleaned,
        "",
        f'Trade Now: <a href="{escape(ref_link)}">here</a>',
        "",
        f"<i>{timestamp or _utc_now()}</i>",
    ]
    return "\n".join(lines)


def format_wallet_tracker_alert(
    alert,
    channel_name: str,
    source_url: str = "",
    timestamp: str | None = None,
    count: int = 1,
) -> str:
    """Build a clean, structured Telegram alert from a parsed Onsight
    wallet-tracker message.

    Mirrors the visual structure of the perp signal formatter (sections
    separated by blank lines, emoji bullets per row) so the Wallet Tracker
    channel reads consistently with the calls channels. All Padre-only
    deeplinks credit Orangie via the URL rewriter that runs separately.

    Excludes competitor brand links (GMGN/Trojan/AXIOM/etc.) entirely.

    When ``count > 1`` the header shows a ``(×N buys)`` indicator and the
    Spent / Amount rows are labeled as 'Total' to make clear the numbers
    are summed across a consolidated batch of rapid same-trader buys.
    """
    direction_icon = "\U0001f7e2" if (alert.action or "").upper() == "BUY" else "\U0001f534"
    pnl_icon = "\U0001f4c8" if alert.pnl_positive else "\U0001f4c9"

    ch = escape(channel_name)
    token = escape(alert.token or "?")
    action = escape(alert.action or "?")

    action_word = "buys" if (alert.action or "").upper() == "BUY" else "sells"
    multi = count > 1

    header = f"<b>{action} {token}</b>"
    if multi:
        header = f"{header} <i>(×{count} {action_word})</i>"

    lines: list[str] = [
        f"{direction_icon} {header}",
    ]

    if alert.platform:
        lines.append(f"<i>via {escape(alert.platform)}</i>")

    lines.append("")
    if source_url:
        lines.append(
            f'\U0001f4e1 Source: Potion '
            f'<a href="{escape(source_url, quote=True)}">#{ch}</a>'
        )
    else:
        lines.append(f"\U0001f4e1 Source: Potion #{ch}")

    if alert.trader:
        lines.append(f"\U0001f464 Trader: <b>{escape(alert.trader)}</b>")
    lines.append("")

    spent_label = "Total Spent" if multi else "Spent"
    amount_label = "Total Amount" if multi else "Amount"
    price_label = "Avg Price" if multi else "Price"

    if alert.spent_sol or alert.spent_usd:
        spent_parts = []
        if alert.spent_sol:
            spent_parts.append(f"<b>{escape(alert.spent_sol)}</b> SOL")
        if alert.spent_usd:
            spent_parts.append(f"(${escape(alert.spent_usd)})")
        lines.append(f"\U0001f4b0 {spent_label}: {' '.join(spent_parts)}")

    if alert.received_amount:
        lines.append(
            f"\U0001f4e6 {amount_label}: <b>{escape(alert.received_amount)}</b> {token}"
        )

    if alert.price:
        display_price = _format_price_for_display(alert.price)
        lines.append(f"\U0001f4b5 {price_label}: <code>${escape(display_price)}</code>")

    if alert.market_cap or alert.age:
        meta = []
        if alert.market_cap:
            meta.append(f"MC: <b>{escape(alert.market_cap)}</b>")
        if alert.age:
            meta.append(f"Age: <b>{escape(alert.age)}</b>")
        lines.append("\U0001f4ca " + "  •  ".join(meta))

    if alert.holds_amount or alert.holds_pct:
        holds_str = ""
        if alert.holds_amount:
            holds_str = f"<b>{escape(alert.holds_amount)}</b>"
        if alert.holds_pct:
            holds_str += f" ({escape(alert.holds_pct)}%)"
        lines.append(f"\U0001f44a Holds: {holds_str.strip()}")

    if alert.pnl:
        lines.append(f"{pnl_icon} PnL: <b>${escape(alert.pnl)}</b>")

    if alert.ca:
        lines.append("")
        lines.append(f"\U0001f3f7️ CA: <code>{escape(alert.ca)}</code>")

    lines.append("")
    lines.append(f"<i>{timestamp or _utc_now()}</i>")

    return "\n".join(lines)


def build_wallet_tracker_keyboard(
    ca: str, ref_code: str = _PADRE_REF_CODE,
) -> InlineKeyboardMarkup | None:
    """Build a single-button keyboard pointing to the token's Padre/Terminal
    trade page with Orangie's referral key. Returns None if no CA is known
    (no point sending a Trade button without a target)."""
    if not ca:
        return None
    url = f"https://trade.padre.gg/trade/solana/{ca}?rk={ref_code}"
    button = InlineKeyboardButton(
        text="\U0001f680 Trade on Terminal", url=url,
    )
    return InlineKeyboardMarkup([[button]])


def label_for_source_type(source_type: str) -> str:
    """Map an internal source_type identifier to a user-facing label."""
    mapping = {
        "perps": "PERPS",
        "memecoin": "MEMECOIN",
    }
    return mapping.get(source_type, source_type.upper())
