"""Venue abstraction for the autotrade engine.

The engine used to talk straight to ``HyperliquidClient`` + ``DelegatesDB``.
To copy the perp-bot signals onto Blofin (where the alts the channel calls
run at 75-150x instead of Hyperliquid's 5x cap), the engine is now written
against this ``Venue`` protocol, and each exchange gets a thin adapter:

  HyperliquidVenue   wraps HyperliquidClient + DelegatesDB (agent keys)
  BlofinVenue        wraps BlofinClient + BlofinCredsDB (API key/secret/pass)

Everything venue-specific lives here: connection lookup, balance, planning a
trade (symbol resolution, price, sizing, leverage cap), and placing it. The
engine keeps all the venue-neutral logic (gating, dedupe, %-sizing, daily
cap, dry-run, DMs). ``uid`` is threaded into every call so an adapter can
fetch that user's own credentials.

A plan carries an opaque ``exec_payload`` so ``place`` gets exactly the
venue-native detail it needs (the Hyperliquid TradePlan, or the Blofin
inst_id + contract count) without the engine knowing either shape.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from src.trading.hyperliquid_client import (
    AccountSnapshot,
    HyperliquidClient,
    HyperliquidError,
)

logger = logging.getLogger(__name__)


class VenueError(Exception):
    """A venue call failed in a way the engine should report, not crash on."""


@dataclass(frozen=True)
class VenueConnection:
    """Whether a user is connected to the venue, plus a display label."""

    is_active: bool
    label: str


@dataclass(frozen=True)
class VenuePlan:
    """Venue-neutral trade plan the engine sizes/dry-runs/DMs against."""

    coin: str
    is_long: bool
    price: float
    size: float           # base-asset units, for display
    leverage: int
    notional_usd: float
    exec_payload: Any = None   # venue-native detail, opaque to the engine


@dataclass(frozen=True)
class VenueResult:
    """Outcome of a placed order, for the success DM."""

    coin: str
    size: float
    sl_ok: bool = True
    tp_ok: bool = True
    ref: str = ""         # order id / oid for the DM


@runtime_checkable
class Venue(Protocol):
    name: str
    supports_risk_guard: bool

    async def get_connection(self, uid: int) -> VenueConnection | None: ...
    async def get_balance(self, uid: int) -> float: ...
    async def plan(
        self, *, pair: str, direction: str, collateral_usdc: float,
        requested_leverage: int, max_leverage: int,
    ) -> VenuePlan | None: ...
    async def place(
        self, uid: int, plan: VenuePlan, *, take_profit: float | None,
        stop_loss: float | None, slippage_bps: int,
    ) -> VenueResult: ...
    async def mark_success(self, uid: int) -> None: ...
    async def mark_failure(self, uid: int, reason: str) -> None: ...
    async def get_account_snapshot(self, uid: int) -> AccountSnapshot | None: ...


def cap_leverage(requested: int, asset_max: int, caller_max: int) -> int:
    """Clamp leverage to [1, min(caller_max, asset_max)].

    A missing/zero request means "use the ceiling", matching the signal
    follower's intent (take the call's leverage, but never above the caps).
    """
    ceiling = min(int(caller_max), int(asset_max))
    if ceiling < 1:
        ceiling = 1
    effective = int(requested) if requested and int(requested) > 0 else ceiling
    return max(1, min(effective, ceiling))


# ---------------------------------------------------------------------------
# Hyperliquid
# ---------------------------------------------------------------------------


class HyperliquidVenue:
    """Adapter over the existing HyperliquidClient + DelegatesDB."""

    name = "hyperliquid"
    supports_risk_guard = True

    def __init__(self, client: HyperliquidClient, delegates_db):
        self._client = client
        self._delegates = delegates_db

    async def get_connection(self, uid: int) -> VenueConnection | None:
        d = await self._delegates.get(uid)
        if d is None:
            return None
        return VenueConnection(is_active=bool(d.is_active), label=d.trader_address)

    async def get_balance(self, uid: int) -> float:
        d = await self._delegates.get(uid)
        if d is None:
            raise VenueError("not connected")
        return await self._client.get_available_usdc(d.trader_address)

    async def plan(
        self, *, pair: str, direction: str, collateral_usdc: float,
        requested_leverage: int, max_leverage: int,
    ) -> VenuePlan | None:
        tp = await self._client.plan_trade(
            pair=pair, direction=direction, collateral_usdc=collateral_usdc,
            requested_leverage=requested_leverage, max_leverage=max_leverage,
        )
        if tp is None:
            return None
        return VenuePlan(
            coin=tp.coin, is_long=tp.is_long, price=tp.price, size=tp.size,
            leverage=tp.leverage, notional_usd=tp.notional_usd, exec_payload=tp,
        )

    async def place(
        self, uid: int, plan: VenuePlan, *, take_profit: float | None,
        stop_loss: float | None, slippage_bps: int,
    ) -> VenueResult:
        d = await self._delegates.get(uid)
        if d is None:
            raise VenueError("not connected")
        agent_key = await self._delegates.get_plaintext_key(uid)
        if not agent_key:
            raise VenueError("no stored key")
        try:
            res = await self._client.place_trade(
                agent_private_key=agent_key, master_address=d.trader_address,
                plan=plan.exec_payload, take_profit=take_profit,
                stop_loss=stop_loss, slippage_bps=slippage_bps,
            )
        except HyperliquidError as e:
            raise VenueError(str(e)) from e
        return VenueResult(
            coin=res.coin, size=res.size, sl_ok=res.sl_ok, tp_ok=res.tp_ok,
            ref=str(res.entry_oid or ""),
        )

    async def mark_success(self, uid: int) -> None:
        await self._delegates.mark_trade_success(uid)

    async def mark_failure(self, uid: int, reason: str) -> None:
        await self._delegates.mark_trade_failure(uid, reason)

    async def get_account_snapshot(self, uid: int) -> AccountSnapshot | None:
        d = await self._delegates.get(uid)
        if d is None:
            return None
        return await self._client.get_account_snapshot(d.trader_address)


# ---------------------------------------------------------------------------
# Blofin
# ---------------------------------------------------------------------------


class BlofinVenue:
    """Adapter over BlofinClient + BlofinCredsDB.

    Collateral is USDT; sizing is in CONTRACTS (client.compute_contracts).
    Leverage is set per-instrument (isolated margin) right before the market
    order, which carries the TP/SL triggers in the same request.
    """

    name = "blofin"
    supports_risk_guard = False   # account snapshot not wired for Blofin yet

    def __init__(self, client, creds_db, *, margin_mode: str = "isolated"):
        self._client = client
        self._creds = creds_db
        self._margin_mode = margin_mode

    async def get_connection(self, uid: int) -> VenueConnection | None:
        rec = await self._creds.get(uid)
        if rec is None:
            return None
        return VenueConnection(is_active=bool(rec.is_active), label="Blofin API key")

    async def _creds_or_raise(self, uid: int):
        c = await self._creds.get_creds(uid)
        if c is None:
            raise VenueError("not connected")
        return c

    async def get_balance(self, uid: int) -> float:
        c = await self._creds_or_raise(uid)
        return await self._client.get_available_usdt(c)

    async def plan(
        self, *, pair: str, direction: str, collateral_usdc: float,
        requested_leverage: int, max_leverage: int,
    ) -> VenuePlan | None:
        base = pair.split("/")[0].strip().upper()
        info = await self._client.resolve_inst_id(base)
        if info is None:
            return None
        leverage = cap_leverage(requested_leverage, info.max_leverage, max_leverage)
        price = await self._client.get_last_price(info.inst_id)
        from src.trading.blofin_client import compute_contracts, signal_side_to_blofin

        contracts = compute_contracts(
            collateral_usdc=collateral_usdc, leverage=leverage, price=price,
            contract_value=info.contract_value, lot_size=info.lot_size,
            min_size=info.min_size,
        )
        if contracts is None:
            return None
        size_base = float(contracts * info.contract_value)
        notional = collateral_usdc * leverage
        return VenuePlan(
            coin=base,
            is_long=direction.strip().upper() == "LONG",
            price=price,
            size=size_base,
            leverage=leverage,
            notional_usd=notional,
            exec_payload={
                "inst_id": info.inst_id,
                "size_contracts": contracts,
                "side": signal_side_to_blofin(direction),
                "margin_mode": self._margin_mode,
            },
        )

    async def place(
        self, uid: int, plan: VenuePlan, *, take_profit: float | None,
        stop_loss: float | None, slippage_bps: int,
    ) -> VenueResult:
        from src.trading.blofin_client import BlofinError

        c = await self._creds_or_raise(uid)
        ep = plan.exec_payload or {}
        inst_id = ep["inst_id"]
        try:
            await self._client.set_leverage(
                c, inst_id, plan.leverage, margin_mode=ep["margin_mode"],
            )
            res = await self._client.place_market_order(
                c, inst_id=inst_id, side=ep["side"],
                size_contracts=ep["size_contracts"],
                take_profit=take_profit, stop_loss=stop_loss,
                margin_mode=ep["margin_mode"],
            )
        except BlofinError as e:
            raise VenueError(str(e)) from e
        # Blofin attaches TP/SL to the same market order; a clean return means
        # they were accepted. Only report the ones we actually asked for.
        return VenueResult(
            coin=plan.coin, size=plan.size,
            sl_ok=stop_loss is not None, tp_ok=take_profit is not None,
            ref=res.order_id,
        )

    async def mark_success(self, uid: int) -> None:
        await self._creds.mark_trade_success(uid)

    async def mark_failure(self, uid: int, reason: str) -> None:
        await self._creds.mark_trade_failure(uid, reason)

    async def get_account_snapshot(self, uid: int) -> AccountSnapshot | None:
        # Not wired for Blofin; supports_risk_guard is False so the engine
        # never calls this. Explicit rather than a misleading empty snapshot.
        raise NotImplementedError(
            "Blofin account snapshot not implemented; risk guard is off for Blofin"
        )


def _decimal_str(value: Decimal) -> str:  # small helper for callers/tests
    return format(value, "f")
