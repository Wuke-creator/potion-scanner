"""Telegram alert formatting.

Pure functions only. No I/O, no async, no Telegram SDK calls. Easy to unit
test against the 28 sample messages in ``signals/samples/``.

Output is HTML-safe for ``parse_mode="HTML"``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.parser import ParsedSignal, Side


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


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
    """
    if not text:
        return ""

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


def build_signal_keyboard(
    ref_link: str, pair: str,
) -> InlineKeyboardMarkup:
    """Build inline buttons for a parsed signal alert.

    Row 1: "Trade now" (ref link) + "Chart" (DexScreener search)
    """
    # Build a DexScreener search URL from the base token (first in the pair)
    base_token = pair.split("/")[0].strip() if "/" in pair else pair.strip()
    chart_url = f"https://dexscreener.com/search?q={base_token}"

    buttons = [
        [
            InlineKeyboardButton(text="\U0001f7e2 Trade now", url=ref_link),
            InlineKeyboardButton(text="\U0001f4ca Chart", url=chart_url),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def format_lifecycle_event(
    label: str,
    raw_message: str,
    ref_link: str,
    channel_name: str,
    source_type_label: str,
    timestamp: str | None = None,
) -> str:
    """Format a lifecycle update (TP hit, breakeven, stop hit, etc.)."""
    cleaned = discord_to_telegram_html(raw_message.strip())
    if len(cleaned) > 1500:
        cleaned = cleaned[:1500] + "..."
    ch = escape(channel_name)

    lines = [
        f"<b>Trade Update: {escape(label)}</b>",
        f"<i>Source: Potion #{ch}</i>",
        "",
        cleaned,
        "",
        f'Trade Now: <a href="{escape(ref_link)}">here</a>',
        "",
        f"<i>{timestamp or _utc_now()}</i>",
    ]
    return "\n".join(lines)


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


def label_for_source_type(source_type: str) -> str:
    """Map an internal source_type identifier to a user-facing label."""
    mapping = {
        "perps": "PERPS",
        "memecoin": "MEMECOIN",
    }
    return mapping.get(source_type, source_type.upper())
