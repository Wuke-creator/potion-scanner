"""Tolerant parser for cabal-chat entry calls (discretionary human posts).

Unlike the Perp Pinger's rigid machine format, cabal entries are typed by a
human and the phrasing drifts. Observed real templates (June-July 2026):

  "Taking a risky short on Ygg/Usdt at cmp ( 0.02240$ )  Dca : 0.02310$
   Sl : 1h candle close above 0.02360$  Final tp : 0.019$  Tp 1 : 0.02160$"

  "Longing rune /usdt at cmp ( 0.3895$ )  Dca : 0.3730$  Sl : 1h candle
   close below 0.3650$  Final tp : 0.4650$+  Tp 1 : 0.4030$"

  "Scalp long BTC at CMP ( 62693$)  Dca: 61730$  Sl : 15min close below
   60970$  Final tp : 67400$"

  "Token : Pyth / Usdt  Entry : 0.04370$  Dca : 0.04240$  Sl : 15min candle
   close below 0.04140$  Final tp : 0.050$"

Design rules:
  - A message only parses as an ENTRY when it has a direction (or a
    Token:/Entry: block), and a stop-loss price. Management chatter
    ("Taking tp 1", "moving sl on be") must NOT parse.
  - "Sl: 1h candle close below X" is a CONDITIONAL stop; we surface the
    price and flag it so the copier knows the hard stop we place is
    slightly stricter than the caller's intent.
  - Leverage is copied when stated ("20x"); usually it isn't ("use low
    lev"), and the caller decides the default.
  - Direction fallback: entries like the Token:/Entry: template omit the
    side; infer it from the stop (SL below entry = long, above = short).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_NUM = r"([0-9][0-9,]*\.?[0-9]*)"

_DIR_LONG = re.compile(r"\b(long(?:ing)?|scalp\s+long)\b", re.IGNORECASE)
_DIR_SHORT = re.compile(r"\b(short(?:ing)?(?:\s+on)?|scalp\s+short)\b", re.IGNORECASE)

# "on Ygg/Usdt", "rune /usdt", "Token : Pyth / Usdt", "BTC at CMP"
_PAIR_SLASH = re.compile(
    r"\b([A-Za-z0-9]{2,12})\s*/\s*(usdt?c?)\b", re.IGNORECASE
)
_TOKEN_LINE = re.compile(
    r"token\s*[:\-]\s*([A-Za-z0-9]{2,12})\b", re.IGNORECASE
)
_TICKER_AT = re.compile(
    r"\b([A-Z0-9]{2,12})\s+at\s+cmp\b", re.IGNORECASE
)

_ENTRY_CMP = re.compile(r"cmp\s*\(\s*\$?" + _NUM + r"\s*\$?\s*\)", re.IGNORECASE)
_ENTRY_LINE = re.compile(r"\bentry\s*[:\-]\s*\$?" + _NUM + r"\s*\$?", re.IGNORECASE)

# The SL line mixes timeframes with the price ("Sl : 1h candle close below
# 0.02360$"): grab the line after "Sl:", then pick the last number that is
# NOT a timeframe token (1h / 15min / 4hr ...).
_SL_LINE = re.compile(r"\bsl\s*[:\-]\s*([^\n]{0,90})", re.IGNORECASE)
_SL_PRICE = re.compile(_NUM + r"(?!\s*(?:h|hr|hrs|m|min|mins)\b)\s*\$?", re.IGNORECASE)
_SL_CONDITIONAL = re.compile(
    r"(candle\s+close|close\s+(above|below))", re.IGNORECASE
)
# management noise: "moving sl on be", "sl to be" — no digits after "sl"
_TP1 = re.compile(r"\btp\s*1\s*[:\-]\s*\$?" + _NUM, re.IGNORECASE)
_TP_FINAL = re.compile(r"\bfinal\s+tp\s*[:\-]\s*\$?" + _NUM, re.IGNORECASE)
_LEV = re.compile(r"\b([0-9]{1,3})\s*x\b", re.IGNORECASE)

# hard vetoes: phrases that mark management updates, not entries
_VETO = re.compile(
    r"\b(taking\s+tp|tp\s+\d\s+(hit|given)|hits?\s+tp|moving\s+sl|sl\s+(on|to)\s+be"
    r"|trimm?(ing|ed)|closing|closed?\s+the|filled\s+dca)\b",
    re.IGNORECASE,
)

_STABLES = {"USDT", "USD", "USDC"}


@dataclass(frozen=True)
class CabalSignal:
    pair: str                      # "PYTH/USDT"
    side: str                      # "LONG" | "SHORT"
    entry: float | None            # None -> at market (CMP with no number)
    stop_loss: float
    stop_is_conditional: bool
    take_profits: list[float] = field(default_factory=list)  # nearest first
    leverage: int | None = None    # None -> caller default
    side_inferred: bool = False    # direction derived from SL vs entry


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def parse_cabal_entry(text: str) -> CabalSignal | None:
    """Parse an entry call; None for anything that isn't one."""
    if not text or len(text) < 20:
        return None
    if _VETO.search(text):
        return None

    sl_line_m = _SL_LINE.search(text)
    if not sl_line_m:
        return None
    sl_segment = sl_line_m.group(1)
    prices = [
        v for m in _SL_PRICE.finditer(sl_segment)
        if (v := _to_float(m.group(1))) is not None
    ]
    if not prices:
        return None
    stop_loss = prices[-1]  # the price sits after the condition prose
    if not stop_loss:
        return None
    stop_conditional = bool(_SL_CONDITIONAL.search(sl_segment))

    # --- pair ---
    base = None
    m = _PAIR_SLASH.search(text)
    if m and m.group(1).upper() not in _STABLES:
        base = m.group(1).upper()
    if base is None:
        m = _TOKEN_LINE.search(text)
        if m and m.group(1).upper() not in _STABLES:
            base = m.group(1).upper()
    if base is None:
        m = _TICKER_AT.search(text)
        if m and m.group(1).upper() not in _STABLES:
            base = m.group(1).upper()
    if base is None:
        return None

    # --- entry price (optional -> market) ---
    entry = None
    m = _ENTRY_CMP.search(text) or _ENTRY_LINE.search(text)
    if m:
        entry = _to_float(m.group(1))

    # --- direction ---
    side = None
    side_inferred = False
    # "short" wins over "long" when both appear ("shorting the long squeeze")
    has_short = bool(_DIR_SHORT.search(text))
    has_long = bool(_DIR_LONG.search(text))
    if has_short and not has_long:
        side = "SHORT"
    elif has_long and not has_short:
        side = "LONG"
    elif entry is not None:
        side = "LONG" if stop_loss < entry else "SHORT"
        side_inferred = True
    if side is None:
        return None

    # sanity: stop must sit on the losing side of the entry
    if entry is not None:
        if side == "LONG" and stop_loss >= entry:
            return None
        if side == "SHORT" and stop_loss <= entry:
            return None

    # --- take profits: nearest first (tp1, then final) ---
    tps: list[float] = []
    m = _TP1.search(text)
    if m:
        v = _to_float(m.group(1))
        if v:
            tps.append(v)
    m = _TP_FINAL.search(text)
    if m:
        v = _to_float(m.group(1))
        if v and v not in tps:
            tps.append(v)
    # TP direction sanity: for a long, all TPs above entry; short below
    if entry is not None and tps:
        good = [
            t for t in tps
            if (t > entry if side == "LONG" else t < entry)
        ]
        tps = good

    # --- leverage: only when explicitly stated ---
    lev = None
    m = _LEV.search(text)
    if m:
        try:
            v = int(m.group(1))
            if 1 <= v <= 150:
                lev = v
        except ValueError:
            pass

    return CabalSignal(
        pair=f"{base}/USDT",
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        stop_is_conditional=stop_conditional,
        take_profits=tps,
        leverage=lev,
        side_inferred=side_inferred,
    )
