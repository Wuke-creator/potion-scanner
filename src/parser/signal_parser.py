"""Full signal field extraction from TRADING SIGNAL ALERT messages."""

import re
from dataclasses import dataclass
from enum import Enum


class RiskLevel(Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Side(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass
class ParsedSignal:
    """Structured representation of a TRADING SIGNAL ALERT."""

    pair: str            # e.g. "ZK/USDT"
    trade_id: int        # e.g. 1286
    risk_level: RiskLevel
    trade_type: str      # "SWING" or "SCALP"
    size: str            # e.g. "1-4%"
    side: Side
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    leverage: int


def _clean(text: str) -> str:
    """Strip Discord markdown (bold, emojis, backticks) from raw text."""
    text = re.sub(r"\*+", "", text)
    text = text.replace("`", "")
    text = re.sub(
        r"[\U0001f300-\U0001f9ff\U00002600-\U000027bf\U0000fe00-\U0000fe0f"
        r"\U0001fa00-\U0001fa6f\U0001fa70-\U0001faff\U0000200d]+",
        "",
        text,
    )
    return text


class SignalParseError(Exception):
    """Raised when a required field cannot be extracted."""


def parse_signal(raw_message: str) -> ParsedSignal:
    """Extract all fields from a TRADING SIGNAL ALERT message.

    Args:
        raw_message: Raw message text (may contain Discord formatting).

    Returns:
        A ParsedSignal with all fields populated.

    Raises:
        SignalParseError: If any required field is missing or malformed.
    """
    text = _clean(raw_message)

    # --- PAIR and Trade ID ---
    m = re.search(r"PAIR[:\s]+(\S+/\S+)\s+#(\d+)", text, re.IGNORECASE)
    if not m:
        raise SignalParseError("Could not extract PAIR and trade ID")
    pair = m.group(1).upper()
    trade_id = int(m.group(2))

    # --- Risk Level ---
    m = re.search(r"\((LOW|MEDIUM|HIGH)\s+RISK\)", text, re.IGNORECASE)
    if not m:
        raise SignalParseError("Could not extract risk level")
    risk_level = RiskLevel(m.group(1).upper())

    # --- Type (SWING / SCALP) ---
    m = re.search(r"TYPE[:\s]+(SWING|SCALP)", text, re.IGNORECASE)
    if not m:
        raise SignalParseError("Could not extract trade type")
    trade_type = m.group(1).upper()

    # --- Size ---
    m = re.search(r"SIZE[:\s]+([\d]+-[\d]+%)", text, re.IGNORECASE)
    if not m:
        raise SignalParseError("Could not extract size")
    size = m.group(1)

    # --- Side ---
    m = re.search(r"SIDE[:\s]+(LONG|SHORT)", text, re.IGNORECASE)
    if not m:
        raise SignalParseError("Could not extract side")
    side = Side(m.group(1).upper())

    # --- Entry ---
    m = re.search(r"ENTRY[:\s]+([\d.]+)", text, re.IGNORECASE)
    if not m:
        raise SignalParseError("Could not extract entry price")
    entry = float(m.group(1))

    # --- Stop Loss ---
    m = re.search(r"SL[:\s]+([\d.]+)", text, re.IGNORECASE)
    if not m:
        raise SignalParseError("Could not extract stop loss")
    stop_loss = float(m.group(1))

    # --- Take Profit targets (in the TAKE PROFIT TARGETS section) ---
    # Match TP lines that have percentages (to distinguish from R:R lines)
    tp_matches = re.findall(
        r"TP(\d)[:\s]+([\d.]+)\s+\([\d.]+%\)", text, re.IGNORECASE
    )
    tp_map: dict[int, float] = {}
    for tp_num, tp_val in tp_matches:
        tp_map[int(tp_num)] = float(tp_val)

    if not all(k in tp_map for k in (1, 2, 3)):
        raise SignalParseError(
            f"Could not extract all TP targets (found: {sorted(tp_map.keys())})"
        )

    # --- Leverage ---
    m = re.search(r"LEVERAGE[:\s]+(\d+)x?", text, re.IGNORECASE)
    if not m:
        raise SignalParseError("Could not extract leverage")
    leverage = int(m.group(1))

    return ParsedSignal(
        pair=pair,
        trade_id=trade_id,
        risk_level=risk_level,
        trade_type=trade_type,
        size=size,
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        tp1=tp_map[1],
        tp2=tp_map[2],
        tp3=tp_map[3],
        leverage=leverage,
    )
