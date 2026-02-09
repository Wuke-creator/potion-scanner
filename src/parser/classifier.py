"""Message type classification — signal, TP hit, prep, update, noise."""

import re
from enum import Enum


class MessageType(Enum):
    """All known Potion Perps message types."""

    SIGNAL_ALERT = "signal_alert"
    ALL_TP_HIT = "all_tp_hit"
    BREAKEVEN = "breakeven"
    STOP_HIT = "stop_hit"
    CANCELED = "canceled"
    TRADE_CLOSED = "trade_closed"
    PREPARATION = "preparation"
    MANUAL_UPDATE = "manual_update"
    NOISE = "noise"


def _strip_markdown(text: str) -> str:
    """Remove Discord markdown formatting (bold, emojis, backticks)."""
    text = re.sub(r"\*+", "", text)
    text = text.replace("`", "")
    # Strip common emoji characters (keep text between them)
    text = re.sub(
        r"[\U0001f300-\U0001f9ff\U00002600-\U000027bf\U0000fe00-\U0000fe0f"
        r"\U0001fa00-\U0001fa6f\U0001fa70-\U0001faff\U0000200d]+",
        "",
        text,
    )
    return text


def classify(raw_message: str) -> MessageType:
    """Classify a raw signal message into its type.

    Uses keyword matching against the cleaned (markdown-stripped) text.
    Rules are ordered from most specific to least specific so that
    unambiguous patterns match first.

    Args:
        raw_message: The raw message text, potentially with Discord formatting.

    Returns:
        The identified MessageType.
    """
    text = _strip_markdown(raw_message).upper()

    # --- Noise (check first — fast reject) ---
    if "@PERP ALERT" in text:
        return MessageType.NOISE

    # --- Lifecycle events (specific keywords) ---
    if "ALL TAKE-PROFIT TARGETS HIT" in text:
        return MessageType.ALL_TP_HIT

    if "BREAK EVEN HIT" in text:
        return MessageType.BREAKEVEN

    if "STOP TARGET HIT" in text:
        return MessageType.STOP_HIT

    if "TRADE CLOSED OUT" in text:
        return MessageType.TRADE_CLOSED

    if re.search(r"CANCEL[LED]", text):
        return MessageType.CANCELED

    # --- Preparation (has "Incoming..." and "Prepare") ---
    if "INCOMING" in text and "PREPARE" in text:
        return MessageType.PREPARATION

    # --- Signal alert (explicit header or has entry/SL/TP fields) ---
    if "TRADING SIGNAL ALERT" in text:
        return MessageType.SIGNAL_ALERT

    # Fallback: some signals arrive without the header but contain key fields
    has_entry = bool(re.search(r"\bENTRY[:\s]", text))
    has_sl = bool(re.search(r"\bSL[:\s]", text))
    has_tp = bool(re.search(r"\bTP\d", text))
    if has_entry and has_sl and has_tp:
        return MessageType.SIGNAL_ALERT

    # --- Manual update (has a pair/trade # but didn't match above) ---
    if re.search(r"PAIR[:\s]", text) or re.search(r"#\d{3,}", text):
        return MessageType.MANUAL_UPDATE

    return MessageType.NOISE
