"""Tesseract-based OCR for signal-bot chart images.

Image-bot channels (Pingu Charts, third-party perp pingers) often post a
chart image with the symbol, side, leverage, entry, market price, and ROI
baked into the image itself. The text caption is usually sparse: just
"WET Update: TP1 here" or similar.

This module downloads the image, preprocesses it for Tesseract, runs OCR,
and returns the cleaned text. A second pass (``parse_ocr_text``) tries to
pull a structured signal-ish dict out of the OCR text using the same regex
patterns the existing perp-bot parser uses.

OCR is intentionally tolerant of failure. If ``pytesseract`` or ``Pillow``
isn't installed (e.g. local dev environments without the system Tesseract
binary), the public functions return None / empty without raising. The
router treats None as "fall back to caption-only handling".
"""
from __future__ import annotations

import asyncio
import io
import logging
import re

import aiohttp

logger = logging.getLogger(__name__)


# Lazy-imported optional deps. We don't want the bot to crash on import
# in environments where Tesseract isn't installed (CI, local dev).
try:
    import pytesseract  # type: ignore
    from PIL import Image, ImageOps  # type: ignore
    _OCR_AVAILABLE = True
except Exception as e:  # pragma: no cover — only triggered without tesseract
    pytesseract = None  # type: ignore
    Image = None  # type: ignore
    ImageOps = None  # type: ignore
    _OCR_AVAILABLE = False
    logger.info("OCR deps unavailable (%s) — image OCR disabled", e)


def ocr_available() -> bool:
    """Returns True if pytesseract + Pillow are importable.

    Does not check whether the system ``tesseract`` binary is on PATH —
    that's a runtime check that happens on the first OCR call. Use this
    only for "should I bother attempting OCR" gating in the router.
    """
    return _OCR_AVAILABLE


# ---------------------------------------------------------------------------
# Image download + preprocess
# ---------------------------------------------------------------------------

# Cap downloaded image size so a malicious or accidentally-huge image
# can't blow up memory. 8MB is well above any chart-card asset and well
# below anything that would slow OCR meaningfully.
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def _download_image(url: str) -> bytes | None:
    """Fetch an image URL into memory. Returns None on any failure."""
    try:
        async with aiohttp.ClientSession(timeout=_DOWNLOAD_TIMEOUT) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(
                        "OCR image download failed: HTTP %d for %s",
                        resp.status, url,
                    )
                    return None
                # Stream-bounded read to avoid loading multi-GB blobs.
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > _MAX_IMAGE_BYTES:
                        logger.warning(
                            "OCR image %s exceeded %d bytes — aborting",
                            url, _MAX_IMAGE_BYTES,
                        )
                        return None
                return bytes(buf)
    except Exception:
        logger.exception("OCR image download crashed for %s", url)
        return None


def _preprocess(raw_bytes: bytes):
    """Open + preprocess image bytes for Tesseract.

    Steps (in order):
      1. Open with PIL.
      2. Convert to RGB then grayscale (drops alpha channels cleanly).
      3. Upscale 2x (Tesseract is much better on >300 DPI equivalents).
      4. Auto-contrast to flatten dark gradient backgrounds (Bybit/Bitget
         chart cards in particular use heavy gradients that Tesseract
         struggles with at default contrast).

    Returns the PIL Image, or None if open fails.
    """
    if not _OCR_AVAILABLE:
        return None
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img = img.convert("RGB")
        img = ImageOps.grayscale(img)
        # Upscale: better small-glyph recognition.
        new_size = (img.width * 2, img.height * 2)
        img = img.resize(new_size, Image.LANCZOS)
        img = ImageOps.autocontrast(img, cutoff=2)
        return img
    except Exception:
        logger.exception("OCR preprocess crashed")
        return None


def _run_tesseract_sync(img) -> str:
    """Synchronous Tesseract call. Wrapped in to_thread by the public
    ``ocr_image_url`` so we don't block the event loop."""
    if not _OCR_AVAILABLE or img is None:
        return ""
    try:
        # PSM 6: assume a uniform block of text (chart cards are basically
        # one block of stat lines). Whitelist alphanumerics + the symbols
        # we actually care about so Tesseract doesn't hallucinate emoji or
        # box-drawing characters.
        config = (
            "--oem 3 --psm 6 "
            "-c tessedit_char_whitelist="
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            "0123456789.,:%/+-x()$ "
        )
        return pytesseract.image_to_string(img, config=config) or ""
    except Exception:
        logger.exception("Tesseract crashed")
        return ""


async def ocr_image_url(url: str) -> str | None:
    """Download + OCR a single image URL.

    Returns the raw OCR text (multi-line), or None if any stage failed.
    The router decides whether to attempt regex-parsing the result.
    """
    if not _OCR_AVAILABLE:
        return None
    raw = await _download_image(url)
    if raw is None:
        return None
    img = _preprocess(raw)
    if img is None:
        return None
    text = await asyncio.to_thread(_run_tesseract_sync, img)
    text = (text or "").strip()
    if text:
        logger.info("OCR succeeded: %d chars from %s", len(text), url)
    else:
        logger.info("OCR yielded empty text for %s", url)
    return text or None


# ---------------------------------------------------------------------------
# OCR text -> structured signal fields
# ---------------------------------------------------------------------------

# Pingu / Bybit / Bitget chart cards typically render fields like:
#   WETUSDT  Short  50x
#   ROI: +198.96%
#   Entry Price: 0.09900
#   Market Price: 0.09495
#   SL: 0.10500
#   TP1: 0.09000
#
# Tesseract output for those usually has the labels intact but may eat
# punctuation (colon/period swaps) and may merge the symbol+side line.
# Regexes below are deliberately permissive on whitespace and casing.

# Symbol extractor: a 3-10 char ticker followed by USD/USDT/USDC, or just
# 3-10 alphanumeric chars on a line by itself when no quote currency
# appears. Strict on left-side word boundary so we don't pick up "ROI" etc.
_SYMBOL_LINE_RE = re.compile(
    r"\b(?P<base>[A-Z][A-Z0-9]{1,9})\s*(?:/\s*)?(?P<quote>USDT?|USDC?)?",
)
_SIDE_RE = re.compile(r"\b(LONG|SHORT)\b", re.IGNORECASE)
_LEVERAGE_RE = re.compile(r"(\d{1,3})\s*x", re.IGNORECASE)
_ENTRY_RE = re.compile(
    r"entry\s*(?:price)?\s*[:=]?\s*([0-9]+(?:[.,][0-9]+)?)",
    re.IGNORECASE,
)
_MARKET_RE = re.compile(
    r"(?:market|current|last)\s*(?:price)?\s*[:=]?\s*([0-9]+(?:[.,][0-9]+)?)",
    re.IGNORECASE,
)
_SL_RE = re.compile(
    r"(?:stop\s*loss|sl)\s*[:=]?\s*([0-9]+(?:[.,][0-9]+)?)",
    re.IGNORECASE,
)
_TP_RE = re.compile(
    r"tp\s*([1-3])\s*[:=]?\s*([0-9]+(?:[.,][0-9]+)?)",
    re.IGNORECASE,
)
_ROI_RE = re.compile(
    r"(?:roi|pnl|profit)\s*[:=]?\s*([+\-]?[0-9]+(?:[.,][0-9]+)?)\s*%",
    re.IGNORECASE,
)


def _parse_float(s: str) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def parse_ocr_text(text: str) -> dict:
    """Best-effort regex extraction of signal-ish fields from OCR text.

    Returns a dict with the fields we managed to find. Always returns a
    dict (never None) — the caller decides which fields are mandatory for
    its use case (e.g. router needs at least ``base`` + ``side`` to treat
    OCR output as a viable new-signal record).

    Keys (all optional):
        base       - ticker like "WET"
        quote      - "USDT" / "USDC" / "USD" / None
        pair       - constructed "WET/USDT" when both base+quote present
        side       - "LONG" / "SHORT"
        leverage   - int
        entry      - float
        market     - float
        stop_loss  - float
        tp1, tp2, tp3 - float
        roi_pct    - float
    """
    out: dict = {}
    if not text:
        return out

    # Side + leverage are typically near the top, on the symbol line, but
    # safe to grep anywhere because the OCR'd card has nothing else with
    # "Long"/"Short" or "<n>x" patterns.
    m = _SIDE_RE.search(text)
    if m:
        out["side"] = m.group(1).upper()
    m = _LEVERAGE_RE.search(text)
    if m:
        try:
            out["leverage"] = int(m.group(1))
        except ValueError:
            pass

    # Numeric fields.
    m = _ENTRY_RE.search(text)
    if m:
        v = _parse_float(m.group(1))
        if v is not None:
            out["entry"] = v
    m = _MARKET_RE.search(text)
    if m:
        v = _parse_float(m.group(1))
        if v is not None:
            out["market"] = v
    m = _SL_RE.search(text)
    if m:
        v = _parse_float(m.group(1))
        if v is not None:
            out["stop_loss"] = v
    for tp_m in _TP_RE.finditer(text):
        idx = tp_m.group(1)
        v = _parse_float(tp_m.group(2))
        if v is not None and idx in ("1", "2", "3"):
            out[f"tp{idx}"] = v
    m = _ROI_RE.search(text)
    if m:
        v = _parse_float(m.group(1))
        if v is not None:
            out["roi_pct"] = v

    # Symbol line: scan line-by-line and pick the first plausible ticker.
    # Skip lines that contain field-label words (entry/market/etc.) so
    # Tesseract row-merge doesn't make us think "ENTRY" is a ticker.
    label_words = (
        "ENTRY", "MARKET", "PRICE", "ROI", "PNL", "PROFIT",
        "STOP", "LOSS", "TP1", "TP2", "TP3", "LEVERAGE",
        "LONG", "SHORT", "SOURCE", "BYBIT", "BITGET", "REFERRAL",
    )
    base = None
    quote = None
    for line in text.splitlines():
        line_clean = line.strip()
        if not line_clean:
            continue
        upper = line_clean.upper()
        # Reject lines that start with a label word — those carry numeric
        # fields, not the symbol.
        if any(upper.startswith(w) for w in label_words):
            continue
        m = _SYMBOL_LINE_RE.search(upper)
        if m:
            cand_base = m.group("base")
            cand_quote = m.group("quote")
            # Reject if the candidate IS one of the label words
            # (e.g. "ENTRY" matches the [A-Z]{2,10} pattern).
            if cand_base in label_words:
                continue
            base = cand_base
            quote = cand_quote
            break

    if base:
        out["base"] = base
    if quote:
        # Normalise USDT/USDC, leave bare USD alone (Ostium-style pairs).
        out["quote"] = quote.upper()
    if base and quote:
        out["pair"] = f"{base}/{quote.upper()}"
    elif base:
        out["pair"] = base

    return out
