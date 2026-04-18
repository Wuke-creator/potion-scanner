"""Telegram alert formatting.

Pure functions only. No I/O, no async, no Telegram SDK calls. Easy to unit
test against the 28 sample messages in ``signals/samples/``.

Output is HTML-safe for ``parse_mode="HTML"``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.parser import ParsedSignal, Side


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


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
    cleaned = escape(raw_message.strip())
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
    """Forward an unparseable but non-noise message verbatim."""
    cleaned = escape(raw_message.strip())
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
