"""HTTP client for the trade-executor sidecar (Node.js + Ostium Builder SDK).

The sidecar runs as a separate Railway service on the private network at
``${potion-trade-executor}.railway.internal:3001``. We talk to it via
plain HTTP since traffic stays inside Railway's private network, gated
by a shared secret header (``X-Executor-Secret``).

Contract:

  POST /trade
    body: {
      trader_address: str,
      delegate_private_key: str,   # plaintext; never logged on either side
      builder_address: str | None, # null -> no fee bps
      builder_fee_bps: int,        # 0..50
      pair_base: str,              # "BTC", "ETH", "SOL", etc.
      direction: "LONG" | "SHORT",
      leverage: int,
      collateral_usdc: float,      # user-input size
      slippage_bps: int,           # user setting
      take_profit: float | None,   # TP1 from the signal
      stop_loss: float | None,
      order_type: "MARKET" | "LIMIT",
      limit_price: float | None,   # required for LIMIT
    }
    -> 200 { tx_hash: str, pair_id: int, status: "submitted" }
    -> 4xx { error: str, code: str }
    -> 5xx { error: str }

  GET /health -> 200 { ok: true, sdk_version: str }
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class ExecutorError(Exception):
    """Raised when /trade returns a non-2xx response or the call fails."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


@dataclass
class TradeResult:
    tx_hash: str
    pair_id: int
    status: str


class TradeExecutorClient:
    """Async client. One aiohttp session reused for the bot lifetime."""

    # How long the bot trusts a fetched Ostium symbol list before
    # refreshing. Ostium adds markets rarely, so 10 min is plenty and
    # keeps load off the executor's read client.
    _SYMBOLS_TTL_SEC = 600

    def __init__(
        self,
        base_url: str,
        shared_secret: str,
        request_timeout_sec: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._shared_secret = shared_secret
        self._timeout = aiohttp.ClientTimeout(total=request_timeout_sec)
        self._session: aiohttp.ClientSession | None = None
        # Last-known-good Ostium symbol set + when we fetched it. None
        # means "never successfully fetched" -> callers fail safe.
        self._symbols: set[str] | None = None
        self._symbols_fetched_at: float = 0.0

    async def open(self) -> None:
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        logger.info("Trade executor client opened: %s", self._base_url)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def health(self) -> dict[str, Any]:
        assert self._session is not None
        async with self._session.get(
            f"{self._base_url}/health",
            headers={"X-Executor-Secret": self._shared_secret},
        ) as resp:
            return await resp.json()

    async def get_supported_symbols(self) -> set[str] | None:
        """Return the set of Ostium-listed base symbols (uppercased).

        Cached for ``_SYMBOLS_TTL_SEC``. On a refresh failure the last
        known-good set is returned (resilient to executor blips). Returns
        ``None`` only when we have never successfully fetched the list —
        callers MUST treat None as "coverage unknown" and fail safe
        (do not show the 1-Tap button).
        """
        import time

        now = time.monotonic()
        fresh = (
            self._symbols is not None
            and (now - self._symbols_fetched_at) < self._SYMBOLS_TTL_SEC
        )
        if fresh:
            return self._symbols

        if self._session is None:
            return self._symbols  # not opened yet; whatever we have (maybe None)

        try:
            async with self._session.get(
                f"{self._base_url}/pairs",
                headers={"X-Executor-Secret": self._shared_secret},
            ) as resp:
                body = await resp.json()
            symbols = body.get("symbols") or []
            if symbols:
                self._symbols = {str(s).upper() for s in symbols}
                self._symbols_fetched_at = now
                logger.info(
                    "Ostium symbol list refreshed: %d markets",
                    len(self._symbols),
                )
            else:
                # Executor returned an empty list (its own fetch failed).
                # Keep whatever we had; don't overwrite a good set with [].
                logger.warning(
                    "Ostium /pairs returned empty (err=%s); keeping cached",
                    body.get("error"),
                )
        except Exception as e:
            logger.warning(
                "Ostium /pairs fetch failed (%s); keeping cached set", e,
            )
        return self._symbols

    async def is_symbol_supported(self, base: str) -> bool | None:
        """True/False if we know coverage, None if coverage is unknown.

        None happens only when we've never fetched the list. Router
        treats None as 'do not show 1-Tap' (fail safe).
        """
        symbols = await self.get_supported_symbols()
        if symbols is None:
            return None
        return base.strip().upper() in symbols

    async def submit_trade(
        self,
        *,
        trader_address: str,
        delegate_private_key: str,
        builder_address: str | None,
        builder_fee_bps: int,
        pair_base: str,
        direction: str,
        leverage: int,
        collateral_usdc: float,
        slippage_bps: int,
        take_profit: float | None,
        stop_loss: float | None,
        order_type: str = "MARKET",
        limit_price: float | None = None,
    ) -> TradeResult:
        assert self._session is not None
        payload = {
            "trader_address": trader_address,
            "delegate_private_key": delegate_private_key,
            "builder_address": builder_address,
            "builder_fee_bps": builder_fee_bps,
            "pair_base": pair_base,
            "direction": direction,
            "leverage": leverage,
            "collateral_usdc": collateral_usdc,
            "slippage_bps": slippage_bps,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
            "order_type": order_type,
            "limit_price": limit_price,
        }
        loggable = {k: v for k, v in payload.items() if k != "delegate_private_key"}
        logger.info("Submitting trade: %s", json.dumps(loggable))
        try:
            async with self._session.post(
                f"{self._base_url}/trade",
                json=payload,
                headers={"X-Executor-Secret": self._shared_secret},
            ) as resp:
                body = await resp.json()
                if resp.status >= 400:
                    raise ExecutorError(
                        body.get("error", f"HTTP {resp.status}"),
                        code=body.get("code"),
                    )
                return TradeResult(
                    tx_hash=body["tx_hash"],
                    pair_id=int(body["pair_id"]),
                    status=body["status"],
                )
        except aiohttp.ClientError as e:
            raise ExecutorError(f"executor unreachable: {e}") from e
