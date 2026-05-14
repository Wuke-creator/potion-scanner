"""Parser for free-form / Perp Pinger style NEW-CALL signals.

Example raw text (verbatim from a Discord Perp Pinger post):

    New Call Detected
    Source: Potion #Perp Calls

    SHORT JUP @ 0.2350
    Taking another risky short on JUP / Usdt
    Entry : 0.2350$
    Dca : 0.2420$
    Sl : 4h candle close above 0.2470$
    Final tp : below 0.1980$
    Risk : 0.5RR

    Called by

    Trade Now: here

Distinct from the structured ``TRADING SIGNAL ALERT`` template (which is
handled by ``src/parser/signal_parser.py``): Perp Pinger format has no
trade id, no risk level enum, no leverage, no TP2/TP3.

Output is a sparse dataclass so downstream callers can record what we
got and fill the rest from user settings / defaults.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class PerpPingerSignal:
    pair: str                            # base ticker, e.g. "JUP"
    side: str                            # "LONG" / "SHORT"
    entry: float
    stop_loss: float | None
    stop_loss_is_conditional: bool       # True when SL text contains "candle close above/below"
    stop_loss_raw: str | None            # original SL line, kept for display
    take_profit: float | None            # the "Final tp" / "Tp" value
    risk_rr: float | None                # informational only


# Header: "SHORT JUP @ 0.2350" / "LONG MOODENG @ 0.5630"
_HEADER_RE = re.compile(
    r"^\s*(?P<side>LONG|SHORT)\s+"
    r"(?P<pair>[A-Z][A-Z0-9]{0,9})\s*"
    r"(?:@\s*(?P<entry>[0-9]+(?:\.[0-9]+)?))?",
    re.IGNORECASE | re.MULTILINE,
)

_ENTRY_FIELD_RE = re.compile(
    r"\bEntry\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)\s*\$?",
    re.IGNORECASE,
)

_SL_FIELD_RE = re.compile(
    r"\bSl\s*[:=]?\s*([^\n\r]+)",
    re.IGNORECASE,
)

_TP_FIELD_RE = re.compile(
    r"\b(?:Final\s+)?Tp\s*[:=]?\s*(?:above\s+|below\s+)?"
    r"([0-9]+(?:\.[0-9]+)?)\s*\$?",
    re.IGNORECASE,
)

_RISK_RE = re.compile(
    r"\bRisk\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)\s*RR\b",
    re.IGNORECASE,
)

_CONDITIONAL_SL_RE = re.compile(
    r"\b(?:candle\s+close\s+)?(?:above|below)\b", re.IGNORECASE,
)

# Last numeric token in an SL line (used when the line is conditional —
# we still want the trigger price even though the wording is qualitative).
_TRAILING_NUMBER_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*\$?\s*$")


def parse_perp_pinger_new_call(text: str) -> PerpPingerSignal | None:
    """Parse a Perp Pinger new-call message.

    Returns ``None`` if the header doesn't match or entry can't be
    determined. Missing TP/SL is acceptable (caller falls back).
    """
    if not text:
        return None

    header = _HEADER_RE.search(text)
    if not header:
        return None

    side = header.group("side").upper()
    pair = header.group("pair").upper()
    header_entry = header.group("entry")

    # Entry: prefer explicit "Entry: X" field, fall back to the value
    # after "@" in the header.
    entry: float | None = None
    field_match = _ENTRY_FIELD_RE.search(text)
    if field_match:
        try:
            entry = float(field_match.group(1))
        except ValueError:
            entry = None
    if entry is None and header_entry:
        try:
            entry = float(header_entry)
        except ValueError:
            entry = None
    if entry is None:
        return None

    # Stop loss: SL line can be a bare number or a conditional phrase like
    # "4h candle close above 0.2470$". We always try to extract a numeric
    # trigger and flag whether the wording was conditional.
    stop_loss: float | None = None
    stop_loss_is_conditional = False
    stop_loss_raw: str | None = None
    sl_match = _SL_FIELD_RE.search(text)
    if sl_match:
        sl_line = sl_match.group(1).strip()
        stop_loss_raw = sl_line
        stop_loss_is_conditional = bool(_CONDITIONAL_SL_RE.search(sl_line))
        trailing = _TRAILING_NUMBER_RE.search(sl_line)
        if trailing:
            try:
                stop_loss = float(trailing.group(1))
            except ValueError:
                stop_loss = None
        else:
            num_match = re.search(r"([0-9]+(?:\.[0-9]+)?)", sl_line)
            if num_match:
                try:
                    stop_loss = float(num_match.group(1))
                except ValueError:
                    stop_loss = None

    take_profit: float | None = None
    tp_match = _TP_FIELD_RE.search(text)
    if tp_match:
        try:
            take_profit = float(tp_match.group(1))
        except ValueError:
            take_profit = None

    risk_rr: float | None = None
    risk_match = _RISK_RE.search(text)
    if risk_match:
        try:
            risk_rr = float(risk_match.group(1))
        except ValueError:
            risk_rr = None

    return PerpPingerSignal(
        pair=pair,
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        stop_loss_is_conditional=stop_loss_is_conditional,
        stop_loss_raw=stop_loss_raw,
        take_profit=take_profit,
        risk_rr=risk_rr,
    )
