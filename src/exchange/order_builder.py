"""Order construction from parsed signals.

Converts a ParsedSignal into Hyperliquid SDK order parameters:
  - Entry order (limit GTC)
  - Stop-loss order (trigger, reduce-only)
  - Take-profit orders (trigger, reduce-only, split across TP1/TP2/TP3)

The SDK handles nonce, signing, and wire format conversion internally.

Hyperliquid constraints discovered during testnet testing:
  - Minimum order value is $10
  - Sizes must be rounded (floor) to avoid float_to_wire precision errors
  - SL/TP trigger orders must be submitted individually after entry fills
    (positionTpsl grouping only accepts 1 SL + 1 TP, not multiple TPs)
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from src.parser.signal_parser import ParsedSignal, Side
from src.utils.symbol_mapper import potion_to_hyperliquid

logger = logging.getLogger(__name__)

# Hyperliquid minimum order value in USD
MIN_ORDER_VALUE_USD = 10.0

# Default size decimal precision per price tier
# Lower-priced coins need more decimals, higher-priced need fewer
_SZ_DECIMALS_BY_PRICE = [
    (0.001, 0),       # sub-penny coins: whole units
    (0.1, 0),         # penny coins: whole units
    (1.0, 1),         # dollar coins: 1 decimal
    (10.0, 2),        # ~$10 coins: 2 decimals
    (100.0, 3),       # ~$100 coins: 3 decimals
    (1000.0, 4),      # ~$1000 coins: 4 decimals
    (float("inf"), 5), # expensive coins: 5 decimals
]


def _sz_decimals(price: float) -> int:
    """Determine appropriate size decimal precision based on coin price."""
    for threshold, decimals in _SZ_DECIMALS_BY_PRICE:
        if price < threshold:
            return decimals
    return 5


def _floor_to(value: float, decimals: int) -> float:
    """Floor a float to N decimal places (avoids rounding up)."""
    factor = 10 ** decimals
    return math.floor(value * factor) / factor


@dataclass
class OrderParams:
    """Parameters for a single Hyperliquid order, ready for the SDK."""

    coin: str
    is_buy: bool
    sz: float
    limit_px: float
    order_type: dict
    reduce_only: bool = False


@dataclass
class TradeOrderSet:
    """Complete set of orders for one trade signal.

    Entry is submitted first. Once filled, SL and TP orders are
    submitted individually as standalone trigger orders.
    """

    coin: str
    trade_id: int
    leverage: int
    is_cross: bool
    entry: OrderParams
    stop_loss: OrderParams
    take_profits: list[OrderParams]


def build_orders(
    signal: ParsedSignal,
    position_size_usd: float,
    tp_split: list[float] | None = None,
    max_leverage: int | None = None,
) -> TradeOrderSet:
    """Convert a parsed signal into a complete set of Hyperliquid orders.

    Args:
        signal: Parsed TRADING SIGNAL ALERT.
        position_size_usd: Total USD value for this position.
        tp_split: Fraction of position to close at each TP level.
            Must sum to 1.0. Defaults to [0.33, 0.33, 0.34].
        max_leverage: Cap leverage at this value regardless of signal.
            None = use the signal's leverage as-is.

    Returns:
        TradeOrderSet with entry, SL, and TP orders ready for submission.

    Raises:
        ValueError: If position_size_usd is below the $10 minimum or tp_split is invalid.
    """
    if tp_split is None:
        tp_split = [0.33, 0.33, 0.34]

    if len(tp_split) != 3 or abs(sum(tp_split) - 1.0) > 0.01:
        raise ValueError(f"tp_split must have 3 values summing to 1.0, got {tp_split}")

    if position_size_usd < MIN_ORDER_VALUE_USD:
        raise ValueError(
            f"Position size ${position_size_usd:.2f} is below Hyperliquid "
            f"minimum of ${MIN_ORDER_VALUE_USD:.2f}"
        )

    # --- Resolve coin name and direction ---
    coin = potion_to_hyperliquid(signal.pair)
    is_long = signal.side == Side.LONG
    leverage = min(signal.leverage, max_leverage) if max_leverage else signal.leverage

    # --- Calculate and round position size ---
    decimals = _sz_decimals(signal.entry)
    sz = _floor_to(position_size_usd / signal.entry, decimals)

    if sz <= 0:
        raise ValueError(
            f"Position size rounds to 0 at {decimals} decimals "
            f"(${position_size_usd} / ${signal.entry})"
        )

    # --- Entry order (limit GTC) ---
    entry = OrderParams(
        coin=coin,
        is_buy=is_long,
        sz=sz,
        limit_px=signal.entry,
        order_type={"limit": {"tif": "Gtc"}},
        reduce_only=False,
    )

    # --- Stop-loss order (trigger, market, reduce-only) ---
    stop_loss = OrderParams(
        coin=coin,
        is_buy=not is_long,
        sz=sz,
        limit_px=signal.stop_loss,
        order_type={
            "trigger": {
                "triggerPx": signal.stop_loss,
                "isMarket": True,
                "tpsl": "sl",
            }
        },
        reduce_only=True,
    )

    # --- Take-profit orders (trigger, market, reduce-only, split sizes) ---
    tp_prices = [signal.tp1, signal.tp2, signal.tp3]
    take_profits = []
    allocated = 0.0
    for i, (tp_price, fraction) in enumerate(zip(tp_prices, tp_split)):
        if i < len(tp_split) - 1:
            tp_sz = _floor_to(sz * fraction, decimals)
            allocated += tp_sz
        else:
            # Last TP gets the remainder to ensure sizes sum exactly to entry
            tp_sz = _floor_to(sz - allocated, decimals)

        take_profits.append(
            OrderParams(
                coin=coin,
                is_buy=not is_long,
                sz=tp_sz,
                limit_px=tp_price,
                order_type={
                    "trigger": {
                        "triggerPx": tp_price,
                        "isMarket": True,
                        "tpsl": "tp",
                    }
                },
                reduce_only=True,
            )
        )

    trade_set = TradeOrderSet(
        coin=coin,
        trade_id=signal.trade_id,
        leverage=leverage,
        is_cross=True,
        entry=entry,
        stop_loss=stop_loss,
        take_profits=take_profits,
    )

    logger.info(
        "Built order set: %s #%d %s %s %s @ %s (lev=%dx, SL=%s, TP=[%s, %s, %s])",
        coin,
        signal.trade_id,
        signal.side.value,
        "BUY" if is_long else "SELL",
        sz,
        signal.entry,
        leverage,
        signal.stop_loss,
        signal.tp1,
        signal.tp2,
        signal.tp3,
    )

    return trade_set


def order_params_to_sdk_request(params: OrderParams) -> dict:
    """Convert an OrderParams to the dict format expected by Exchange.order().

    This is the OrderRequest TypedDict the SDK expects:
        {"coin": str, "is_buy": bool, "sz": float, "limit_px": float,
         "order_type": OrderType, "reduce_only": bool}
    """
    return {
        "coin": params.coin,
        "is_buy": params.is_buy,
        "sz": params.sz,
        "limit_px": params.limit_px,
        "order_type": params.order_type,
        "reduce_only": params.reduce_only,
    }
