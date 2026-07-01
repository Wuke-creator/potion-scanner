"""Async REST client for the Blofin futures API.

Blofin replaces Ostium as the autotrade venue: it lists the long tail of
alt/perp markets the perp bot actually calls, and its API keys cannot hold
withdrawal permission (Read / Trade / Transfer only), so a leaked key can
trade but never move funds off the account. Keys can also be IP-locked.

This client is pure Python (no Node sidecar). It talks to the public
market endpoints unauthenticated and the private trading endpoints with
per-user HMAC-signed requests.

Signing (verified against docs.blofin.com):

    prehash = requestPath + method + timestamp + nonce + body
    hexsig  = HMAC_SHA256(secret, prehash).hexdigest()
    sign    = base64( hexsig.encode() )        # base64 of the HEX string

Headers on private calls:

    ACCESS-KEY, ACCESS-SIGN, ACCESS-TIMESTAMP (ms), ACCESS-NONCE,
    ACCESS-PASSPHRASE, Content-Type: application/json

Order size is denominated in CONTRACTS, not USDC. One contract is worth
``contractValue`` units of the base currency (e.g. 0.001 BTC for BTC-USDT).
``compute_contracts`` converts a USDC collateral + leverage into a
contract count using the live price and rounds down to the lot size.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Mapping

import aiohttp

logger = logging.getLogger(__name__)


PROD_BASE_URL = "https://openapi.blofin.com"
DEMO_BASE_URL = "https://demo-trading-openapi.blofin.com"


class BlofinError(Exception):
    """Raised when a Blofin call fails or returns a non-zero code."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BlofinCreds:
    """Per-user API credentials. Never log these."""

    api_key: str
    api_secret: str
    passphrase: str


@dataclass(frozen=True)
class InstrumentInfo:
    inst_id: str
    contract_value: Decimal   # base-currency units per contract
    min_size: Decimal         # minimum order size in contracts
    lot_size: Decimal         # contract size increment
    max_leverage: int
    state: str                # "live" when tradeable


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    raw: dict[str, Any]


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without network)
# ---------------------------------------------------------------------------


def sign_request(
    secret: str,
    method: str,
    request_path: str,
    timestamp: str,
    nonce: str,
    body: str = "",
) -> str:
    """Return the ACCESS-SIGN value for a Blofin request.

    ``request_path`` must include the query string for GET calls, exactly
    as sent. ``method`` is upper-case. ``body`` is the JSON string for POST
    or "" for GET. The result is base64 of the hex HMAC-SHA256 digest.
    """
    prehash = f"{request_path}{method.upper()}{timestamp}{nonce}{body}"
    digest = hmac.new(
        secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    return base64.b64encode(digest.encode("utf-8")).decode("ascii")


def _fmt_decimal(value: Decimal) -> str:
    """Format a Decimal as a plain fixed-point string (no sci notation)."""
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def compute_contracts(
    *,
    collateral_usdc: float,
    leverage: int,
    price: float,
    contract_value: Decimal,
    lot_size: Decimal,
    min_size: Decimal,
) -> Decimal | None:
    """Convert a USDC collateral + leverage into a contract count.

    notional = collateral * leverage
    base_qty = notional / price
    contracts = base_qty / contract_value, floored to the lot size.

    Returns None when the resulting size is below the instrument minimum
    (caller must skip the trade rather than send a rejectable order).
    """
    if collateral_usdc <= 0 or leverage <= 0 or price <= 0:
        return None
    if contract_value <= 0 or lot_size <= 0:
        return None
    try:
        notional = Decimal(str(collateral_usdc)) * Decimal(int(leverage))
        base_qty = notional / Decimal(str(price))
        raw_contracts = base_qty / contract_value
    except (InvalidOperation, ZeroDivisionError):
        return None
    # Floor to the nearest lot-size increment.
    steps = (raw_contracts / lot_size).to_integral_value(rounding=ROUND_DOWN)
    contracts = steps * lot_size
    if contracts < min_size or contracts <= 0:
        return None
    return contracts


def signal_side_to_blofin(direction: str) -> str:
    """Map a signal direction (LONG/SHORT) to Blofin order side."""
    return "buy" if direction.strip().upper() == "LONG" else "sell"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class BlofinClient:
    """One shared aiohttp session; per-request credentials.

    Public market data (instruments, tickers) needs no credentials and is
    cached. Private calls take a ``BlofinCreds`` and sign per request.
    """

    _INSTRUMENTS_TTL_SEC = 600

    def __init__(
        self,
        base_url: str = PROD_BASE_URL,
        *,
        request_timeout_sec: float = 15.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=request_timeout_sec)
        self._session: aiohttp.ClientSession | None = None
        self._instruments: dict[str, InstrumentInfo] | None = None
        self._instruments_fetched_at: float = 0.0

    async def open(self) -> None:
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        logger.info("Blofin client opened: %s", self._base_url)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ---- low-level request helpers ----------------------------------------

    @staticmethod
    def _check_body(body: Mapping[str, Any]) -> dict[str, Any]:
        """Validate a Blofin envelope and return it; raise on non-zero code."""
        code = str(body.get("code", ""))
        if code and code != "0":
            raise BlofinError(body.get("msg") or f"code {code}", code=code)
        return dict(body)

    async def _public_get(
        self, path: str, params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        assert self._session is not None
        async with self._session.get(
            f"{self._base_url}{path}", params=params or {},
        ) as resp:
            body = await resp.json()
        return self._check_body(body)

    async def _private_request(
        self,
        creds: BlofinCreds,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body_obj: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert self._session is not None
        method = method.upper()
        request_path = path
        if query:
            # Deterministic order so the signed path matches the sent path.
            qs = "&".join(f"{k}={query[k]}" for k in query)
            request_path = f"{path}?{qs}"
        body_str = "" if body_obj is None else json.dumps(body_obj, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        sign = sign_request(
            creds.api_secret, method, request_path, timestamp, nonce, body_str,
        )
        headers = {
            "ACCESS-KEY": creds.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-NONCE": nonce,
            "ACCESS-PASSPHRASE": creds.passphrase,
            "Content-Type": "application/json",
        }
        url = f"{self._base_url}{request_path}"
        try:
            if method == "GET":
                async with self._session.get(url, headers=headers) as resp:
                    body = await resp.json()
            else:
                async with self._session.post(
                    url, headers=headers, data=body_str,
                ) as resp:
                    body = await resp.json()
        except aiohttp.ClientError as e:
            raise BlofinError(f"blofin unreachable: {e}") from e
        return self._check_body(body)

    # ---- public market data ------------------------------------------------

    async def get_instruments(self) -> dict[str, InstrumentInfo] | None:
        """Return live instruments keyed by instId, cached for the TTL.

        Returns None only when we have never successfully fetched — callers
        MUST treat None as "coverage unknown" and fail safe (skip the trade).
        """
        now = time.monotonic()
        fresh = (
            self._instruments is not None
            and (now - self._instruments_fetched_at) < self._INSTRUMENTS_TTL_SEC
        )
        if fresh:
            return self._instruments
        if self._session is None:
            return self._instruments
        try:
            body = await self._public_get("/api/v1/market/instruments")
            parsed: dict[str, InstrumentInfo] = {}
            for row in body.get("data") or []:
                try:
                    info = InstrumentInfo(
                        inst_id=str(row["instId"]),
                        contract_value=Decimal(str(row.get("contractValue", "0"))),
                        min_size=Decimal(str(row.get("minSize", "0"))),
                        lot_size=Decimal(str(row.get("lotSize", "0"))),
                        max_leverage=int(float(row.get("maxLeverage", "0") or 0)),
                        state=str(row.get("state", "")),
                    )
                except (KeyError, InvalidOperation, ValueError):
                    continue
                parsed[info.inst_id] = info
            if parsed:
                self._instruments = parsed
                self._instruments_fetched_at = now
                logger.info("Blofin instruments refreshed: %d markets", len(parsed))
        except BlofinError as e:
            logger.warning("Blofin instruments fetch failed (%s); keeping cache", e)
        return self._instruments

    async def resolve_inst_id(self, base: str) -> InstrumentInfo | None:
        """Map a signal base symbol (e.g. "BTC") to a live USDT perp.

        Returns None when the market is not listed or coverage is unknown,
        so the caller skips rather than guesses.
        """
        instruments = await self.get_instruments()
        if not instruments:
            return None
        inst_id = f"{base.strip().upper()}-USDT"
        info = instruments.get(inst_id)
        if info is None or info.state != "live":
            return None
        return info

    async def get_last_price(self, inst_id: str) -> float:
        body = await self._public_get(
            "/api/v1/market/tickers", {"instId": inst_id},
        )
        rows = body.get("data") or []
        if not rows:
            raise BlofinError(f"no ticker for {inst_id}")
        return float(rows[0]["last"])

    # ---- private trading ---------------------------------------------------

    async def get_available_usdt(self, creds: BlofinCreds) -> float:
        """Available USDT in the futures account (basis for %-of-balance)."""
        body = await self._private_request(
            creds, "GET", "/api/v1/asset/balances",
            query={"accountType": "futures"},
        )
        for row in body.get("data") or []:
            if str(row.get("currency", "")).upper() == "USDT":
                return float(row.get("available", 0) or 0)
        return 0.0

    async def set_leverage(
        self, creds: BlofinCreds, inst_id: str, leverage: int,
        *, margin_mode: str = "isolated",
    ) -> None:
        await self._private_request(
            creds, "POST", "/api/v1/trade/set-leverage",
            body_obj={
                "instId": inst_id,
                "leverage": str(int(leverage)),
                "marginMode": margin_mode,
            },
        )

    async def place_market_order(
        self,
        creds: BlofinCreds,
        *,
        inst_id: str,
        side: str,
        size_contracts: Decimal,
        take_profit: float | None = None,
        stop_loss: float | None = None,
        margin_mode: str = "isolated",
    ) -> OrderResult:
        """Place a market order with optional attached TP/SL.

        TP/SL use market execution on trigger (order price "-1"), matching
        the manual 1-Tap flow's "fires immediately" semantics.
        """
        body_obj: dict[str, Any] = {
            "instId": inst_id,
            "marginMode": margin_mode,
            "side": side,
            "orderType": "market",
            "size": _fmt_decimal(size_contracts),
        }
        if take_profit is not None:
            body_obj["tpTriggerPrice"] = str(take_profit)
            body_obj["tpOrderPrice"] = "-1"
        if stop_loss is not None:
            body_obj["slTriggerPrice"] = str(stop_loss)
            body_obj["slOrderPrice"] = "-1"

        body = await self._private_request(
            creds, "POST", "/api/v1/trade/order", body_obj=body_obj,
        )
        data = body.get("data")
        row = data[0] if isinstance(data, list) and data else (data or {})
        if isinstance(row, dict):
            inner_code = str(row.get("code", "0"))
            if inner_code and inner_code != "0":
                raise BlofinError(
                    row.get("msg") or f"order code {inner_code}",
                    code=inner_code,
                )
            order_id = str(row.get("orderId", ""))
        else:
            order_id = ""
        return OrderResult(order_id=order_id, raw=body)
