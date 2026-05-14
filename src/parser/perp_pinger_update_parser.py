"""Parser for free-form Perp Pinger UPDATE / CLOSE / STOP / BE messages.

Examples (verbatim from real Discord posts):

  "Closing lab here in full closed"
  "CLOSED LAB"
  "Stopped JUP out at break even"
  "Move SL to BE on AAVE"
  "AAVE TP1 hit, took partial"
  "Moving SL on BTC to 65000"

The structured Potion Perps Bot lifecycle templates (TP1 HIT TICKER /
SL HIT TICKER / etc.) already get classified upstream in
``src/parser/classifier.py``; this parser is specifically for the
free-form variants posted by Perp Pinger that the classifier marks as
NOISE and which would otherwise forward verbatim without enrichment.

Output is a small dataclass: kind (what happened) + pair (which ticker).
The router uses these to look up the original signal in open_signals_db
and render the structured lifecycle card.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class UpdateKind(str, Enum):
    CLOSE = "close"
    STOP = "stop"
    BREAKEVEN = "be"
    TP_HIT = "tp_hit"
    ADJUST = "adjust"


@dataclass
class PerpPingerUpdate:
    kind: UpdateKind
    pair: str
    raw_keyword: str   # the phrase that matched, for logging/debug


# Action words we know are NEVER tickers (filter false positives from the
# generic ticker capture regex). Mirrors the blocklist in formatter.py.
_TICKER_BLOCKLIST = frozenset({
    "SHORT", "LONG", "TP", "TP1", "TP2", "TP3", "SL", "DCA",
    "EP", "CMP", "BE", "HIT", "UPDATE", "STOP", "STOPPED",
    "CLOSED", "CLOSE", "CLOSING", "ALERT", "RISKY", "SCALP",
    "SWING", "MOVE", "MOVING", "ADD", "ADJUST", "TOOK", "PARTIAL",
    "FULL", "HERE", "OUT", "AT", "TO", "ON", "TAKEN", "GRATS",
    "WINS", "MANUAL", "TRADE", "NOW", "FROM", "THE", "ORIGINAL",
})


def _candidate_ticker(token: str) -> bool:
    """A token looks like a ticker if it's 2-10 alphanumerics, starts with
    a letter, all upper-case after upper-casing, and not in the blocklist.
    """
    if not token:
        return False
    up = token.upper().strip()
    if not (2 <= len(up) <= 10):
        return False
    if not up[0].isalpha():
        return False
    if not all(c.isalnum() for c in up):
        return False
    if up in _TICKER_BLOCKLIST:
        return False
    return True


def _extract_ticker_near(
    text: str, keyword_match: re.Match,
) -> str | None:
    """Find the most plausible ticker in the vicinity of a matched keyword.

    Strategy: scan a window of ~30 chars before and after the keyword for
    any token that looks like a ticker. Prefer tokens that are explicitly
    upper-case in the source (a true ticker like LAB or AAVE), then
    fall back to upper-casing any candidate.
    """
    start, end = keyword_match.start(), keyword_match.end()
    window = text[max(0, start - 40): min(len(text), end + 40)]
    # Tokens separated by non-word chars.
    raw_tokens = re.findall(r"[A-Za-z][A-Za-z0-9]*", window)
    # First pass: explicitly upper-case tokens (the real signal of a ticker).
    for tok in raw_tokens:
        if tok.isupper() and _candidate_ticker(tok):
            return tok.upper()
    # Second pass: any candidate, just upper-case it.
    for tok in raw_tokens:
        if _candidate_ticker(tok):
            return tok.upper()
    return None


# Ordered list of (kind, keyword_regex). Earlier patterns win when more
# than one matches (e.g. "Stopped JUP out, breakeven" hits both STOP and
# BE — STOP is the more important signal).
_PATTERNS: tuple[tuple[UpdateKind, re.Pattern], ...] = (
    # CLOSE: "closed X", "closing X", "close out X", "closed lab", "closing lab here"
    (UpdateKind.CLOSE, re.compile(
        r"\b(?:CLOSED|CLOSING|CLOSE\s*OUT|FULL\s+CLOSE[D]?|SOLD)\b",
        re.IGNORECASE,
    )),
    # STOP: "stopped X out", "stopped X", "SL hit X" (free-form)
    (UpdateKind.STOP, re.compile(
        r"\b(?:STOPPED|STOP\s*HIT|STOPPED\s+OUT|SL\s+HIT)\b",
        re.IGNORECASE,
    )),
    # TP_HIT: "TP1 hit on X", "X TP1 hit", "took TP on X"
    (UpdateKind.TP_HIT, re.compile(
        r"\b(?:TP[123]?\s+HIT|TOOK\s+TP|PARTIAL\s+TP|TAKEN\s+TP)\b",
        re.IGNORECASE,
    )),
    # BREAKEVEN: "moved SL to BE on X", "X SL to BE", "to break even"
    (UpdateKind.BREAKEVEN, re.compile(
        r"\b(?:SL\s+TO\s+BE|MOVE[D]?\s+SL\s+TO\s+BE|TO\s+BREAK\s*EVEN|"
        r"BREAK\s*EVEN|MOVED\s+TO\s+BE)\b",
        re.IGNORECASE,
    )),
    # ADJUST: catch-all "moving SL on X", "adjusting X", "update X"
    (UpdateKind.ADJUST, re.compile(
        r"\b(?:MOVING\s+SL|ADJUSTING|UPDATE\s+ON|UPDATING)\b",
        re.IGNORECASE,
    )),
)


def parse_perp_pinger_update(text: str) -> PerpPingerUpdate | None:
    """Detect a Perp Pinger update / close / stop / BE / TP-hit message.

    Returns ``None`` if no update keyword matches or no plausible ticker
    can be extracted near the matched keyword.
    """
    if not text:
        return None

    for kind, pattern in _PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        ticker = _extract_ticker_near(text, m)
        if not ticker:
            continue
        return PerpPingerUpdate(
            kind=kind,
            pair=ticker,
            raw_keyword=m.group(0),
        )
    return None


# Convenient label for routing into format_lifecycle_event. The router
# combines this with the channel name and pair to build the Telegram
# message header.
LABELS: dict[UpdateKind, str] = {
    UpdateKind.CLOSE: "Manual Close",
    UpdateKind.STOP: "Stopped Out",
    UpdateKind.BREAKEVEN: "Moved SL to BE",
    UpdateKind.TP_HIT: "Take-Profit Hit",
    UpdateKind.ADJUST: "Manual Update",
}
