"""Order construction from parsed signals.

Converts a ParsedSignal into Hyperliquid SDK order parameters:
  - Entry order (limit GTC)
  - Stop-loss order (trigger, reduce-only)
  - Take-profit orders (trigger, reduce-only, split across TP1/TP2/TP3)

The SDK handles nonce, signing, and wire format conversion internally.
"""

import logging
from dataclasses import dataclass

from src.parser.signal_parser import ParsedSignal, Side
from src.utils.symbol_mapper import potion_to_hyperliquid

logger = logging.getLogger(__name__)


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

    Includes the entry order, stop-loss, and take-profit orders.
    These are submitted together via bulk_orders with grouping='normalTpsl'.
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
    """
    if tp_split is None:
        tp_split = [0.33, 0.33, 0.34]

    if len(tp_split) != 3 or abs(sum(tp_split) - 1.0) > 0.01:
        raise ValueError(f"tp_split must have 3 values summing to 1.0, got {tp_split}")

    # --- Resolve coin name and direction ---
    coin = potion_to_hyperliquid(signal.pair)
    is_long = signal.side == Side.LONG
    leverage = min(signal.leverage, max_leverage) if max_leverage else signal.leverage

    # --- Calculate position size in coin units ---
    # size = USD allocation / entry price
    sz = position_size_usd / signal.entry

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
        is_buy=not is_long,  # opposite side to close
        sz=sz,
        limit_px=signal.stop_loss,  # SDK uses limit_px for trigger orders too
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
    for i, (tp_price, fraction) in enumerate(zip(tp_prices, tp_split)):
        tp_sz = sz * fraction
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
        "Built order set: %s #%d %s %s %.4f @ %.6f (lev=%dx, SL=%.6f, TP=[%.6f, %.6f, %.6f])",
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
    """Convert an OrderParams to the dict format expected by Exchange.bulk_orders().

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
