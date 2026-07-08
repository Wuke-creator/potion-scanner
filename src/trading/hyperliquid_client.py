"""Async wrapper around the Hyperliquid Python SDK for autotrade.

Hyperliquid is the autotrade venue: a no-KYC on-chain perp DEX with an
agent-wallet model where the API wallet key can place orders but cannot
withdraw funds. Each user connects (master account address + agent
private key); the address is used for queries, the key for signing.

The SDK is synchronous, so every network call is run in a worker thread
via ``asyncio.to_thread`` to keep the bot's event loop responsive.

Sizing rules (verified against the upstream Hyperliquid bot + SDK):
  - size is in base-asset units, floored to the asset's ``szDecimals``
  - prices are rounded to 5 significant figures (Hyperliquid tick rule)
  - minimum order value is $10 notional
  - leverage is capped by both the caller's max and the asset's maxLeverage

The pure helpers (``floor_to``, ``round_price``, ``compute_size``) carry no
SDK dependency and are unit-tested without it. SDK imports are lazy so the
module loads even where the SDK is not installed.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass

from src.utils.symbol_mapper import potion_to_hyperliquid

logger = logging.getLogger(__name__)

MIN_ORDER_VALUE_USD = 10.0
_DEFAULT_LEVERAGE = 5


class HyperliquidError(Exception):
    """Raised when a Hyperliquid call fails or an order is rejected."""


@dataclass(frozen=True)
class TradePlan:
    """A fully-resolved, pre-flight trade ready to submit (or dry-run)."""

    coin: str
    is_long: bool
    price: float          # mid used for sizing
    size: float           # base-asset units, floored to szDecimals
    leverage: int         # capped by caller max + asset max
    notional_usd: float   # size * price
    sz_decimals: int = 0  # asset size precision, for splitting the TP ladder


@dataclass(frozen=True)
class AccountSnapshot:
    """Account state the risk guard needs: total value + open exposure."""

    account_value: float           # marginSummary.accountValue (USD)
    positions: dict[str, float]    # coin -> abs position notional (USD)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without the SDK)
# ---------------------------------------------------------------------------


def floor_to(value: float, decimals: int) -> float:
    """Floor a float to N decimal places (never rounds up into a bigger size)."""
    if decimals < 0:
        decimals = 0
    factor = 10 ** decimals
    return math.floor(value * factor) / factor


def round_price(price: float, sig_figs: int = 5) -> float:
    """Round a price to N significant figures (Hyperliquid's tick rule)."""
    if price <= 0:
        return 0.0
    magnitude = math.floor(math.log10(abs(price)))
    factor = 10 ** (sig_figs - 1 - magnitude)
    return round(price * factor) / factor


def compute_size(
    notional_usd: float,
    price: float,
    sz_decimals: int,
    *,
    min_notional: float = MIN_ORDER_VALUE_USD,
) -> float | None:
    """Convert a USD notional into a base-asset size, floored to szDecimals.

    Returns None when the floored size is zero or its notional is below the
    exchange minimum, so the caller skips rather than sending a reject.
    """
    if notional_usd <= 0 or price <= 0:
        return None
    size = floor_to(notional_usd / price, sz_decimals)
    if size <= 0:
        return None
    if size * price < min_notional:
        return None
    return size


def cap_leverage(requested: int, asset_max: int | None, caller_max: int | None) -> int:
    lev = requested if requested and requested > 0 else _DEFAULT_LEVERAGE
    if caller_max:
        lev = min(lev, caller_max)
    if asset_max:
        lev = min(lev, asset_max)
    return max(1, int(lev))


# SDK response parsing (shape: {"response":{"data":{"statuses":[...]}}}) --------


def _statuses(result: dict) -> list:
    resp = result.get("response") if isinstance(result, dict) else None
    data = resp.get("data") if isinstance(resp, dict) else None
    statuses = data.get("statuses") if isinstance(data, dict) else None
    return statuses if isinstance(statuses, list) else []


def _first_error(result: dict) -> str | None:
    st = _statuses(result)
    if not st:
        resp = result.get("response") if isinstance(result, dict) else None
        return resp if isinstance(resp, str) and resp else None
    return st[0].get("error") if isinstance(st[0], dict) else None


def _first_oid(result: dict) -> int | None:
    st = _statuses(result)
    if not st or not isinstance(st[0], dict):
        return None
    for key in ("resting", "filled"):
        if key in st[0]:
            return st[0][key].get("oid")
    return None


def _first_avg_px(result: dict) -> float | None:
    st = _statuses(result)
    if st and isinstance(st[0], dict) and "filled" in st[0]:
        try:
            return float(st[0]["filled"].get("avgPx"))
        except (TypeError, ValueError):
            return None
    return None


@dataclass(frozen=True)
class TradeSubmitResult:
    coin: str
    size: float
    entry_oid: int | None
    entry_avg_px: float | None
    sl_ok: bool
    tp_ok: bool
    raw_entry: dict


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class HyperliquidClient:
    """Shared read client + per-user signing. All SDK calls run off-loop."""

    _META_TTL_SEC = 600

    def __init__(self, network: str = "mainnet"):
        self._network = network
        self._info = None
        self._base_url: str | None = None
        self._meta: dict[str, dict] | None = None
        self._meta_fetched_at: float = 0.0

    def _ensure_info(self):
        if self._info is None:
            from hyperliquid.info import Info
            from hyperliquid.utils import constants
            self._base_url = (
                constants.MAINNET_API_URL
                if self._network == "mainnet"
                else constants.TESTNET_API_URL
            )
            self._info = Info(base_url=self._base_url, skip_ws=True)
        return self._info

    async def get_asset_meta(self) -> dict[str, dict] | None:
        """{coin: {"szDecimals": int, "maxLeverage": int}}, cached. None if never fetched."""
        import time
        now = time.monotonic()
        if self._meta is not None and (now - self._meta_fetched_at) < self._META_TTL_SEC:
            return self._meta

        def _fetch():
            info = self._ensure_info()
            raw = info.meta()
            return {a["name"]: a for a in raw["universe"]}

        try:
            meta = await asyncio.to_thread(_fetch)
            if meta:
                self._meta = meta
                self._meta_fetched_at = now
                logger.info("Hyperliquid meta refreshed: %d assets", len(meta))
        except Exception as e:  # noqa: BLE001 - keep last-good cache on blip
            logger.warning("Hyperliquid meta fetch failed (%s); keeping cache", e)
        return self._meta

    async def resolve_coin(self, pair: str) -> str | None:
        """Map a signal pair to a live Hyperliquid coin, or None if unlisted."""
        meta = await self.get_asset_meta()
        if not meta:
            return None
        try:
            return potion_to_hyperliquid(pair, available_coins=meta)
        except ValueError:
            return None

    async def get_mid_price(self, coin: str) -> float | None:
        def _fetch():
            info = self._ensure_info()
            return info.all_mids()
        try:
            mids = await asyncio.to_thread(_fetch)
        except Exception as e:  # noqa: BLE001
            logger.warning("all_mids failed for %s: %s", coin, e)
            return None
        px = mids.get(coin) if isinstance(mids, dict) else None
        if px is None:
            return None
        try:
            return float(px)
        except (TypeError, ValueError):
            return None

    async def get_available_usdc(self, master_address: str) -> float:
        """Tradable USDC for the account (basis for %-of-balance sizing).

        Sums the perps clearinghouse ``withdrawable`` and the spot
        clearinghouse USDC balance (total minus hold). Under Hyperliquid's
        unified account mode the trading USDC lives in the SPOT
        clearinghouse while the perps state reads 0, so reading perps
        alone under-reports unified accounts to $0. For a classic
        (separated) account this can over-report by the spot portion; the
        failure mode there is a rejected order (reported to the user),
        never an oversized fill.
        """
        def _fetch():
            info = self._ensure_info()
            perp = info.user_state(master_address)
            spot = info.spot_user_state(master_address)
            return perp, spot
        try:
            perp_state, spot_state = await asyncio.to_thread(_fetch)
        except Exception as e:  # noqa: BLE001
            logger.warning("balance fetch failed for %s: %s", master_address, e)
            return 0.0
        total = 0.0
        try:
            total += float(perp_state.get("withdrawable", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            pass
        try:
            for bal in (spot_state or {}).get("balances", []):
                if str(bal.get("coin", "")).upper() == "USDC":
                    held = float(bal.get("hold", 0) or 0)
                    total += max(0.0, float(bal.get("total", 0) or 0) - held)
                    break
        except (TypeError, ValueError, AttributeError):
            pass
        return total

    async def get_account_snapshot(self, master_address: str) -> "AccountSnapshot | None":
        """Account value + per-coin open notionals for the risk guard.

        One user_state call. Returns None on ANY failure so the guard can
        fail closed; never raises into the engine.
        """
        def _fetch():
            info = self._ensure_info()
            return info.user_state(master_address), info.spot_user_state(master_address)
        try:
            state, spot_state = await asyncio.to_thread(_fetch)
        except Exception as e:  # noqa: BLE001
            logger.warning("user_state failed for %s: %s", master_address, e)
            return None
        try:
            account_value = float(
                (state.get("marginSummary") or {}).get("accountValue", 0) or 0
            )
            # Unified accounts keep trading USDC in the SPOT clearinghouse while
            # perps accountValue reads 0; include spot USDC so the exposure
            # denominator isn't 0 (which would fail the guard closed and block
            # every trade on a perfectly funded account). Over-counting on a
            # classic account only understates exposure (more permissive), never
            # the reverse.
            for _bal in (spot_state or {}).get("balances", []):
                if str(_bal.get("coin", "")).upper() == "USDC":
                    account_value += float(_bal.get("total", 0) or 0)
                    break
            positions: dict[str, float] = {}
            for entry in state.get("assetPositions") or []:
                pos = (entry or {}).get("position") or {}
                coin = pos.get("coin")
                if not coin:
                    continue
                try:
                    notional = abs(float(pos.get("positionValue", 0) or 0))
                except (TypeError, ValueError):
                    continue
                if notional > 0:
                    positions[coin] = positions.get(coin, 0.0) + notional
            return AccountSnapshot(account_value=account_value, positions=positions)
        except (TypeError, ValueError, AttributeError) as e:
            logger.warning("could not parse user_state for %s: %s", master_address, e)
            return None

    async def plan_trade(
        self,
        *,
        pair: str,
        direction: str,
        collateral_usdc: float,
        requested_leverage: int,
        max_leverage: int,
    ) -> TradePlan | None:
        """Resolve coin + price + size + capped leverage. None if not tradeable."""
        coin = await self.resolve_coin(pair)
        if coin is None:
            return None
        meta = await self.get_asset_meta()
        if not meta or coin not in meta:
            return None
        info = meta[coin]
        price = await self.get_mid_price(coin)
        if not price:
            return None
        leverage = cap_leverage(
            requested_leverage,
            _safe_int(info.get("maxLeverage")),
            max_leverage,
        )
        sz_decimals = _safe_int(info.get("szDecimals"), 0) or 0
        notional = collateral_usdc * leverage
        size = compute_size(notional, price, sz_decimals)
        if size is None:
            return None
        return TradePlan(
            coin=coin,
            is_long=direction.strip().upper() == "LONG",
            price=price,
            size=size,
            leverage=leverage,
            notional_usd=size * price,
            sz_decimals=sz_decimals,
        )

    async def place_trade(
        self,
        *,
        agent_private_key: str,
        master_address: str,
        plan: TradePlan,
        tp_legs: list[tuple[float, float]],
        stop_loss: float | None,
        slippage_bps: int,
    ) -> TradeSubmitResult:
        """Set leverage, market-in via IOC, then attach reduce-only SL + TP ladder.

        ``tp_legs`` is a list of (trigger_price, size) reduce-only take-profits,
        the scale-out ladder (e.g. thirds at TP1/TP2/TP3). The stop is one
        full-size reduce-only order that clamps to whatever remains as TPs fill.

        Runs the whole sequence in one worker thread. Raises HyperliquidError
        if the entry is rejected; SL/TP failures are reported as flags, not
        raised (the position is already open and must not be left unmanaged
        silently — caller should surface a partial result).
        """
        slip = max(0.0, slippage_bps / 10_000.0)

        def _submit() -> TradeSubmitResult:
            import eth_account
            from hyperliquid.exchange import Exchange
            from hyperliquid.utils import constants

            base_url = (
                constants.MAINNET_API_URL
                if self._network == "mainnet"
                else constants.TESTNET_API_URL
            )
            wallet = eth_account.Account.from_key(agent_private_key)
            exchange = Exchange(
                wallet=wallet, base_url=base_url, account_address=master_address,
            )

            # Isolated margin per position; cap already applied in the plan.
            try:
                exchange.update_leverage(plan.leverage, plan.coin, is_cross=False)
            except Exception as e:  # noqa: BLE001
                raise HyperliquidError(f"set leverage failed: {e}") from e

            # Market-in via an aggressive IOC limit crossing the book.
            if plan.is_long:
                entry_px = round_price(plan.price * (1 + slip))
            else:
                entry_px = round_price(plan.price * (1 - slip))
            entry = exchange.order(
                plan.coin, plan.is_long, plan.size, entry_px,
                {"limit": {"tif": "Ioc"}}, reduce_only=False,
            )
            err = _first_error(entry)
            if err:
                raise HyperliquidError(f"entry rejected: {err}")

            sl_ok = tp_ok = True
            if stop_loss:
                sl_px = round_price(stop_loss)
                try:
                    res = exchange.order(
                        plan.coin, not plan.is_long, plan.size, sl_px,
                        {"trigger": {"triggerPx": sl_px, "isMarket": True, "tpsl": "sl"}},
                        reduce_only=True,
                    )
                    sl_ok = _first_error(res) is None
                except Exception as e:  # noqa: BLE001
                    logger.warning("SL submit failed for %s: %s", plan.coin, e)
                    sl_ok = False
            for tp_price, tp_size in tp_legs:
                if tp_size <= 0:
                    continue
                tp_px = round_price(tp_price)
                try:
                    res = exchange.order(
                        plan.coin, not plan.is_long, tp_size, tp_px,
                        {"trigger": {"triggerPx": tp_px, "isMarket": True, "tpsl": "tp"}},
                        reduce_only=True,
                    )
                    if _first_error(res) is not None:
                        tp_ok = False
                except Exception as e:  # noqa: BLE001
                    logger.warning("TP submit failed for %s @ %s: %s", plan.coin, tp_px, e)
                    tp_ok = False

            return TradeSubmitResult(
                coin=plan.coin,
                size=plan.size,
                entry_oid=_first_oid(entry),
                entry_avg_px=_first_avg_px(entry),
                sl_ok=sl_ok,
                tp_ok=tp_ok,
                raw_entry=entry,
            )

        return await asyncio.to_thread(_submit)


def _safe_int(value, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
