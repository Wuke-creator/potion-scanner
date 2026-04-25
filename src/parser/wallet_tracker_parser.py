"""Parser for Onsight-style wallet-tracker alerts in the Potion Discord.

Onsight Alerts posts wallet activity as a multi-section message:

    🆕 🟢 BUY <token> on <platform>
    [link icon] <trader name>

    [link] <trader> swapped <amount_sol> SOL for <amount_token>
    ($<usd>) <token> @$<price>
    👊 Holds: <holds> (<pct>%) 📈 uPnL: $<pnl>

    #<token> | MC: $<mcap> | Seen: <age> | <bunch of tool links>
    <CA on its own line>
    TX | <tx-handler tools>

    <token>: <more tool links>

The text contains heavy Discord markdown (``**bold**``, ``[label](<url>)``,
``<https://...>`` no-preview wrappers). This parser strips the markdown
first, then extracts the canonical fields with regex. Robust to missing
fields: returns whatever it finds, leaves the rest blank.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class WalletTrackerAlert:
    """Structured fields from one Onsight wallet-tracker message.

    All string fields are plain (no Discord markdown). Empty string when
    the field is missing from the source message.
    """

    action: str = ""         # "BUY" / "SELL"
    token: str = ""          # e.g. "shithead"
    platform: str = ""       # e.g. "PumpSwap"
    trader: str = ""         # e.g. "euris"
    spent_sol: str = ""      # e.g. "0.98"
    spent_usd: str = ""      # e.g. "84.61"
    received_amount: str = ""  # e.g. "1,083,372.08"
    price: str = ""          # e.g. "0.0000780"
    holds_amount: str = ""   # e.g. "13.57M"
    holds_pct: str = ""      # e.g. "1.36"
    pnl: str = ""            # e.g. "+504.23" or "-1.61"
    pnl_positive: bool = False
    market_cap: str = ""     # e.g. "$78.09K"
    age: str = ""            # e.g. "38m"
    ca: str = ""             # contract address (base58)
    raw_content: str = ""    # original Discord message
    parsed_ok: bool = False  # True if at least the action+token+ca were found


# ---------------------------------------------------------------------------
# Markdown stripping (run before regex extraction so patterns are simpler)
# ---------------------------------------------------------------------------

_MD_LINK_ANGLE = re.compile(r"\[([^\]]+)\]\(<[^>]+>\)")
_MD_LINK_PLAIN = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_BOLD = re.compile(r"\*\*([^*\n]+?)\*\*")
_MD_UNDERLINE = re.compile(r"__([^_\n]+?)__")
_MD_ITALIC_STAR = re.compile(r"(?<![*\w])\*([^*\n]+?)\*(?!\w)")
_MD_ITALIC_UNDER = re.compile(r"(?<![_\w])_([^_\n]+?)_(?!\w)")
_MD_STRIKE = re.compile(r"~~([^~\n]+?)~~")
_MD_CODE_INLINE = re.compile(r"`([^`\n]+?)`")
_MD_BARE_URL = re.compile(r"<(https?://[^>\s]+)>")


def _strip_markdown(text: str) -> str:
    """Convert Discord markdown to plain text. Drops link URLs but keeps
    the link label so downstream regex can match values like token names
    that were originally hyperlinked."""
    text = _MD_LINK_ANGLE.sub(r"\1", text)
    text = _MD_LINK_PLAIN.sub(r"\1", text)
    text = _MD_BOLD.sub(r"\1", text)
    text = _MD_UNDERLINE.sub(r"\1", text)
    text = _MD_ITALIC_STAR.sub(r"\1", text)
    text = _MD_ITALIC_UNDER.sub(r"\1", text)
    text = _MD_STRIKE.sub(r"\1", text)
    text = _MD_CODE_INLINE.sub(r"\1", text)
    text = _MD_BARE_URL.sub(r"\1", text)
    return text


# ---------------------------------------------------------------------------
# Field-extraction regexes (run on stripped text)
# ---------------------------------------------------------------------------

# "🟢 BUY <token> on <platform>" or "🔴 SELL <token> on <platform>".
# Tolerates 🆕 prefix.
_ACTION_RE = re.compile(
    r"(?:\U0001f195\s*)?[\U0001f7e2\U0001f534]\s*(BUY|SELL)\s+(\S+)\s+on\s+(\S+)",
)

# Swap line: "swapped 0.98 SOL [(+fee 0.02 SOL)] for 1,083,372.08 ($84.61) shithead @$0.0000780"
_SWAP_RE = re.compile(
    r"swapped\s+([\d,.]+)\s+SOL"
    r"(?:\s*\(\+fee\s+[\d,.]+\s+SOL\))?"
    r"\s+for\s+([\d,.]+)\s+\(\$([\d,.]+)\)\s+(\S+)\s+@\$([\d,.]+(?:e[-+]?\d+)?)",
    re.IGNORECASE,
)

# "Holds: 13.57M (1.36%)"
_HOLDS_RE = re.compile(
    r"Holds:\s*([\d,.]+[KMBT]?)\s*\(([\d,.]+)%\)", re.IGNORECASE,
)

# "uPnL: $+504.23" or "uPnL: $-1.61"
_PNL_RE = re.compile(
    r"uPnL:\s*\$?([+-])([\d,.]+)", re.IGNORECASE,
)

# "MC: $78.09K"
_MC_RE = re.compile(r"MC:\s*(\$[\d,.]+[KMBT]?)", re.IGNORECASE)

# "Seen: 38m" / "Seen: 2h" / "Seen: 1d"
_AGE_RE = re.compile(r"Seen:\s*(\d+[smhd])", re.IGNORECASE)

# Trader is the diamond-prefixed name on the line by itself (after action).
# In stripped text this looks like one of:
#   ◆ euris
#   🔷 groovy
#   ◇ trader_name
# We match any line that starts with a diamond-ish glyph then a name.
_TRADER_RE = re.compile(
    r"^\s*[◆◇⬥⬦\U0001f539\U0001f538\U0001f537\U0001f536]\s*([\w\s.\-]+?)\s*$",
    re.MULTILINE,
)

# Solana address (base58, 32-44 chars, no l/I/0/O). Match within a Solscan
# token URL first, then on its own line, then anywhere.
_SOLSCAN_TOKEN_RE = re.compile(
    r"solscan\.io/token/([1-9A-HJ-NP-Za-km-z]{32,44})", re.IGNORECASE,
)
_BASE58_LINE_RE = re.compile(
    r"^\s*([1-9A-HJ-NP-Za-km-z]{32,44})\s*$", re.MULTILINE,
)
_BASE58_ANY_RE = re.compile(r"\b([1-9A-HJ-NP-Za-km-z]{32,44})\b")


def _extract_ca(stripped: str, raw: str) -> str:
    """Find the contract address in the message.

    Tries (in order): solscan.io/token/<CA>, standalone base58 line,
    any base58 token of plausible length. Searches the RAW (markdown-still-
    intact) text first so we don't lose the CA inside a stripped link.
    """
    m = _SOLSCAN_TOKEN_RE.search(raw) or _SOLSCAN_TOKEN_RE.search(stripped)
    if m:
        return m.group(1)
    m = _BASE58_LINE_RE.search(stripped)
    if m:
        return m.group(1)
    m = _BASE58_ANY_RE.search(stripped)
    if m:
        return m.group(1)
    return ""


def parse_wallet_tracker(content: str) -> WalletTrackerAlert:
    """Parse one Onsight wallet-tracker alert. Returns whatever fields are
    extractable. Sets ``parsed_ok=True`` only if action, token, and CA are
    all present."""
    raw = content or ""
    alert = WalletTrackerAlert(raw_content=raw)
    if not raw:
        return alert

    stripped = _strip_markdown(raw)

    # Action / token / platform
    m = _ACTION_RE.search(stripped)
    if m:
        alert.action = m.group(1).upper()
        alert.token = m.group(2).strip().lstrip("#")
        alert.platform = m.group(3).strip()

    # Swap line
    m = _SWAP_RE.search(stripped)
    if m:
        alert.spent_sol = m.group(1)
        alert.received_amount = m.group(2)
        alert.spent_usd = m.group(3)
        # m.group(4) is the token name appearing inside the swap sentence;
        # we already have it from the action line, so ignore here.
        alert.price = m.group(5)

    # Holds
    m = _HOLDS_RE.search(stripped)
    if m:
        alert.holds_amount = m.group(1)
        alert.holds_pct = m.group(2)

    # PnL
    m = _PNL_RE.search(stripped)
    if m:
        sign = m.group(1)
        alert.pnl = f"{sign}{m.group(2)}"
        alert.pnl_positive = sign == "+"

    # Market cap
    m = _MC_RE.search(stripped)
    if m:
        alert.market_cap = m.group(1)

    # Age
    m = _AGE_RE.search(stripped)
    if m:
        alert.age = m.group(1)

    # Trader name. Skip lines that look like the action header (already
    # stripped by then but defensively).
    for trader_match in _TRADER_RE.finditer(stripped):
        candidate = trader_match.group(1).strip()
        # Skip if this is the action line itself (contains BUY/SELL keyword
        # or is the same as the token).
        if "BUY" in candidate.upper() or "SELL" in candidate.upper():
            continue
        if candidate.lower() == alert.token.lower():
            continue
        alert.trader = candidate
        break

    # Contract address (search both stripped + raw for solscan/token URL)
    alert.ca = _extract_ca(stripped, raw)

    alert.parsed_ok = bool(alert.action and alert.token and alert.ca)
    return alert
