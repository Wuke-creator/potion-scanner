"""Read-only Hyperliquid public-data client for the wallet copy stack.

Hyperliquid exposes every wallet's state publicly. This client wraps the
four reads the wallet scout + watcher need:

  get_leaderboard()          stats-data leaderboard (~40k rows: pnl/roi/vlm
                             per day/week/month/allTime window per wallet)
  get_clearinghouse_state()  live open positions + margin summary for one
                             wallet (POST /info type=clearinghouseState)
  get_user_fills()           recent trade history for one wallet
                             (POST /info type=userFills)
  get_candles()              OHLC candles for ATR-based stop derivation
                             (POST /info type=candleSnapshot)

Read-only by construction: no keys, no signing, nothing here can place an
order or move funds. One shared aiohttp session, same lifecycle shape as
BlofinClient (open()/close()). All parsers are tolerant of missing fields
because these are undocumented-but-stable public endpoints.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

INFO_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

# Leaderboard window keys as the endpoint names them.
WINDOWS = ("day", "week", "month", "allTime")


class HLInfoError(Exception):
    """A public-data read failed in a way the caller should handle."""


def _f(value: Any, default: float = 0.0) -> float:
    """Tolerant float: the API returns numbers as strings."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class LeaderboardRow:
    """One wallet on the leaderboard, windows flattened to dicts."""

    address: str
    account_value: float
    pnl: dict[str, float] = field(default_factory=dict)     # window -> $
    roi: dict[str, float] = field(default_factory=dict)     # window -> fraction
    volume: dict[str, float] = field(default_factory=dict)  # window -> $
    display_name: str = ""


@dataclass(frozen=True)
class WalletPosition:
    """One open position from clearinghouseState."""

    coin: str
    szi: float            # signed size in base units (+long / -short)
    entry_px: float
    leverage: float
    notional: float       # positionValue, $
    margin_used: float    # $

    @property
    def is_long(self) -> bool:
        return self.szi > 0


@dataclass(frozen=True)
class WalletState:
    """Live account state for one wallet."""

    account_value: float
    positions: dict[str, WalletPosition]   # coin -> position


def parse_leaderboard_row(raw: dict) -> LeaderboardRow | None:
    """Parse one leaderboard row; None when it has no usable address.

    windowPerformances arrives as a list of [window_name, {pnl, roi, vlm}]
    pairs; a plain dict form is accepted too for safety.
    """
    address = str(raw.get("ethAddress") or raw.get("address") or "").strip()
    if not address:
        return None
    perf = raw.get("windowPerformances") or []
    if isinstance(perf, dict):
        items = list(perf.items())
    else:
        items = [(p[0], p[1]) for p in perf if isinstance(p, (list, tuple)) and len(p) == 2]
    pnl: dict[str, float] = {}
    roi: dict[str, float] = {}
    volume: dict[str, float] = {}
    for window, vals in items:
        if not isinstance(vals, dict):
            continue
        pnl[str(window)] = _f(vals.get("pnl"))
        roi[str(window)] = _f(vals.get("roi"))
        volume[str(window)] = _f(vals.get("vlm"))
    return LeaderboardRow(
        address=address,
        account_value=_f(raw.get("accountValue")),
        pnl=pnl,
        roi=roi,
        volume=volume,
        display_name=str(raw.get("displayName") or ""),
    )


def parse_clearinghouse_state(raw: dict) -> WalletState:
    """Flatten a clearinghouseState response into WalletState."""
    margin = raw.get("marginSummary") or {}
    positions: dict[str, WalletPosition] = {}
    for ap in raw.get("assetPositions") or []:
        pos = (ap or {}).get("position") or {}
        coin = str(pos.get("coin") or "").strip()
        szi = _f(pos.get("szi"))
        if not coin or szi == 0:
            continue
        lev = pos.get("leverage")
        lev_value = _f(lev.get("value")) if isinstance(lev, dict) else _f(lev)
        positions[coin] = WalletPosition(
            coin=coin,
            szi=szi,
            entry_px=_f(pos.get("entryPx")),
            leverage=lev_value,
            notional=abs(_f(pos.get("positionValue"))),
            margin_used=_f(pos.get("marginUsed")),
        )
    return WalletState(
        account_value=_f(margin.get("accountValue")),
        positions=positions,
    )


class HyperliquidInfoClient:
    """Shared-session client for Hyperliquid public data. Read-only."""

    def __init__(
        self,
        *,
        info_url: str = INFO_URL,
        leaderboard_url: str = LEADERBOARD_URL,
        request_timeout_sec: float = 30.0,
    ):
        self._info_url = info_url
        self._leaderboard_url = leaderboard_url
        self._timeout = aiohttp.ClientTimeout(total=request_timeout_sec)
        self._session: aiohttp.ClientSession | None = None

    async def open(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _get_json(self, url: str) -> Any:
        assert self._session is not None, "call open() first"
        try:
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    raise HLInfoError(f"GET {url} -> HTTP {resp.status}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise HLInfoError(f"GET {url} failed: {e}") from e

    async def _post_info(self, payload: dict) -> Any:
        assert self._session is not None, "call open() first"
        try:
            async with self._session.post(self._info_url, json=payload) as resp:
                if resp.status != 200:
                    raise HLInfoError(
                        f"info {payload.get('type')} -> HTTP {resp.status}",
                    )
                return await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise HLInfoError(f"info {payload.get('type')} failed: {e}") from e

    async def get_leaderboard(self) -> list[LeaderboardRow]:
        body = await self._get_json(self._leaderboard_url)
        raw_rows = (
            body.get("leaderboardRows") if isinstance(body, dict) else body
        ) or []
        rows: list[LeaderboardRow] = []
        for raw in raw_rows:
            if not isinstance(raw, dict):
                continue
            row = parse_leaderboard_row(raw)
            if row is not None:
                rows.append(row)
        return rows

    async def get_clearinghouse_state(self, address: str) -> WalletState:
        body = await self._post_info(
            {"type": "clearinghouseState", "user": address},
        )
        if not isinstance(body, dict):
            raise HLInfoError(f"unexpected clearinghouseState body for {address}")
        return parse_clearinghouse_state(body)

    async def get_user_fills(self, address: str) -> list[dict]:
        body = await self._post_info({"type": "userFills", "user": address})
        return body if isinstance(body, list) else []

    async def get_candles(
        self, coin: str, *, interval: str = "1h",
        start_ms: int, end_ms: int,
    ) -> list[dict]:
        body = await self._post_info({
            "type": "candleSnapshot",
            "req": {
                "coin": coin, "interval": interval,
                "startTime": int(start_ms), "endTime": int(end_ms),
            },
        })
        return body if isinstance(body, list) else []
