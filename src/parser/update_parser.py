"""Parser for TP hits, SL updates, trade cancellations, and other lifecycle events."""

import re
from dataclasses import dataclass

from .signal_parser import _clean


class UpdateParseError(Exception):
    """Raised when a required field cannot be extracted from an update message."""


# ---------------------------------------------------------------------------
# Parsed data classes — one per message type
# ---------------------------------------------------------------------------

@dataclass
class TpHit:
    """A single take-profit target hit (e.g. TP1, TP2)."""

    pair: str
    trade_id: int
    tp_number: int       # which TP was hit (1, 2, or 3)
    profit_pct: float
    period: str          # e.g. "23 Minutes"


@dataclass
class AllTpHit:
    """All take-profit targets reached."""

    pair: str
    trade_id: int
    profit_pct: float
    period: str          # e.g. "9 Hours 39 Minutes"


@dataclass
class Breakeven:
    """Price returned to entry after a TP was secured."""

    pair: str
    trade_id: int
    tp_secured: int      # which TP was hit before BE (e.g. 1)


@dataclass
class StopHit:
    """Stop-loss target hit."""

    pair: str
    trade_id: int
    loss_pct: float      # negative value, e.g. -77.7


@dataclass
class Canceled:
    """Trade canceled before or after entry."""

    trade_id: int
    pair: str | None     # may be absent (e.g. "Trade #1268 Canceled")
    reason: str


@dataclass
class TradeClosed:
    """Trade manually closed out (often after a specific TP)."""

    pair: str
    trade_id: int
    detail: str          # free-text detail, e.g. "AFTER REACHING TAKE PROFIT 2"


@dataclass
class Preparation:
    """Heads-up message — do NOT execute."""

    trade_id: int
    pair: str
    side: str | None     # LONG / SHORT (always present so far)
    entry: float | None  # sometimes missing
    leverage: int | None # sometimes missing


@dataclass
class ManualUpdate:
    """Free-form manual instruction from the signal provider."""

    trade_id: int | None
    pair: str | None
    instruction: str     # the full cleaned message text


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _extract_pair(text: str) -> str | None:
    """Try to extract a pair like 'BTC/USDT' from text."""
    m = re.search(r"PAIR[:\s]*(\S+/\S+)", text, re.IGNORECASE)
    return m.group(1).upper() if m else None


def _extract_trade_id(text: str) -> int | None:
    """Try to extract a trade number like #1286 or 'Trade #1286'."""
    m = re.search(r"#(\d{3,})", text)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Per-type parsers
# ---------------------------------------------------------------------------

def parse_tp_hit(raw: str) -> TpHit:
    text = _clean(raw)

    pair = _extract_pair(text)
    trade_id = _extract_trade_id(text)
    if not pair or trade_id is None:
        raise UpdateParseError("TP_HIT: could not extract pair/trade_id")

    m = re.search(r"TP TARGET (\d) HIT", text, re.IGNORECASE)
    if not m:
        raise UpdateParseError("TP_HIT: could not extract TP number")
    tp_number = int(m.group(1))

    m = re.search(r"PROFIT[:\s]+([\d.]+)%", text, re.IGNORECASE)
    if not m:
        raise UpdateParseError("TP_HIT: could not extract profit %")
    profit_pct = float(m.group(1))

    m = re.search(r"PERIOD[:\s]+(.+)", text, re.IGNORECASE)
    period = m.group(1).strip() if m else ""

    return TpHit(pair=pair, trade_id=trade_id, tp_number=tp_number,
                 profit_pct=profit_pct, period=period)


def parse_all_tp_hit(raw: str) -> AllTpHit:
    text = _clean(raw)

    pair = _extract_pair(text)
    trade_id = _extract_trade_id(text)
    if not pair or trade_id is None:
        raise UpdateParseError("ALL_TP_HIT: could not extract pair/trade_id")

    m = re.search(r"PROFIT[:\s]+([\d.]+)%", text, re.IGNORECASE)
    if not m:
        raise UpdateParseError("ALL_TP_HIT: could not extract profit %")
    profit_pct = float(m.group(1))

    m = re.search(r"PERIOD[:\s]+(.+)", text, re.IGNORECASE)
    period = m.group(1).strip() if m else ""

    return AllTpHit(pair=pair, trade_id=trade_id, profit_pct=profit_pct, period=period)


def parse_breakeven(raw: str) -> Breakeven:
    text = _clean(raw)

    pair = _extract_pair(text)
    trade_id = _extract_trade_id(text)
    if not pair or trade_id is None:
        raise UpdateParseError("BREAKEVEN: could not extract pair/trade_id")

    m = re.search(r"TP(\d)", text)
    tp_secured = int(m.group(1)) if m else 1

    return Breakeven(pair=pair, trade_id=trade_id, tp_secured=tp_secured)


def parse_stop_hit(raw: str) -> StopHit:
    text = _clean(raw)

    pair = _extract_pair(text)
    trade_id = _extract_trade_id(text)
    if not pair or trade_id is None:
        raise UpdateParseError("STOP_HIT: could not extract pair/trade_id")

    m = re.search(r"LOSS[:\s]+([-\d.]+)%", text, re.IGNORECASE)
    if not m:
        raise UpdateParseError("STOP_HIT: could not extract loss %")
    loss_pct = float(m.group(1))

    return StopHit(pair=pair, trade_id=trade_id, loss_pct=loss_pct)


def parse_canceled(raw: str) -> Canceled:
    text = _clean(raw)

    trade_id = _extract_trade_id(text)
    if trade_id is None:
        raise UpdateParseError("CANCELED: could not extract trade_id")

    pair = _extract_pair(text)
    # Fallback: "CANCEL DOT/USDT #1249 ..." format (no PAIR: prefix)
    if not pair:
        m = re.search(r"CANCEL\w*\s+(\S+/\S+)", text, re.IGNORECASE)
        if m:
            pair = m.group(1).upper()

    # Reason: parenthesized text, or everything after the first line
    m = re.search(r"\(([^)]+)\)", text)
    if m:
        reason = m.group(1).strip()
    else:
        lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
        reason = " ".join(lines[1:]) if len(lines) > 1 else ""

    return Canceled(trade_id=trade_id, pair=pair, reason=reason)


def parse_trade_closed(raw: str) -> TradeClosed:
    text = _clean(raw)

    pair = _extract_pair(text)
    trade_id = _extract_trade_id(text)
    if not pair or trade_id is None:
        raise UpdateParseError("TRADE_CLOSED: could not extract pair/trade_id")

    # Grab the detail line (usually the last non-empty line)
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    detail = lines[-1] if len(lines) > 1 else ""

    return TradeClosed(pair=pair, trade_id=trade_id, detail=detail)


def parse_preparation(raw: str) -> Preparation:
    text = _clean(raw)

    trade_id = _extract_trade_id(text)
    if trade_id is None:
        raise UpdateParseError("PREPARATION: could not extract trade_id")

    pair = _extract_pair(text)
    if not pair:
        raise UpdateParseError("PREPARATION: could not extract pair")

    m = re.search(r"SIDE[:\s]+(LONG|SHORT)", text, re.IGNORECASE)
    side = m.group(1).upper() if m else None

    m = re.search(r"ENTRY[:\s]+([\d.]+)", text, re.IGNORECASE)
    entry = float(m.group(1)) if m else None

    m = re.search(r"LEVERAGE[:\s]*(\d+)", text, re.IGNORECASE)
    leverage = int(m.group(1)) if m else None

    return Preparation(
        trade_id=trade_id, pair=pair, side=side, entry=entry, leverage=leverage
    )


@dataclass
class SlUpdate:
    """Parsed stop-loss adjustment instruction."""

    trade_id: int
    new_price: float


def parse_sl_update(raw: str) -> SlUpdate | None:
    """Try to extract a stop-loss move instruction from free-text.

    Returns an SlUpdate if the message looks like an SL adjustment,
    or None if it doesn't match any known pattern.

    Recognised patterns (case-insensitive):
        - "Move SL to 1985"
        - "Adjust stop loss to 0.025"
        - "New SL: 1985"
        - "SL → 1985" / "SL -> 1985"
        - "Stop loss 1985"
        - "Change SL to 1985"
    """
    text = _clean(raw)

    trade_id = _extract_trade_id(text)
    if trade_id is None:
        return None

    patterns = [
        r"(?:move|adjust|change|update|set)\s+(?:stop\s*loss|sl)\s+(?:to\s+)?([\d.]+)",
        r"(?:new|updated?)\s+(?:stop\s*loss|sl)[:\s]+([\d.]+)",
        r"(?:stop\s*loss|sl)\s*(?:→|->|to)\s*([\d.]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return SlUpdate(trade_id=trade_id, new_price=float(m.group(1)))

    return None


def parse_manual_update(raw: str) -> ManualUpdate:
    text = _clean(raw)

    trade_id = _extract_trade_id(text)
    pair = _extract_pair(text)
    instruction = text.strip()

    return ManualUpdate(trade_id=trade_id, pair=pair, instruction=instruction)
