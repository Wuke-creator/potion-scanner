"""Exchange-level trade lifecycle operations.

Handles submitting order sets, canceling orders, and moving stop-losses.
All operations are recorded in the TradeDatabase.
"""

import logging
from typing import Any

from src.exchange.hyperliquid import HyperliquidClient
from src.exchange.order_builder import OrderParams, TradeOrderSet, order_params_to_sdk_request
from src.state.models import OrderStatus, OrderType, TradeStatus
from src.state.database import TradeDatabase

logger = logging.getLogger(__name__)


class OrderSubmissionError(Exception):
    """Raised when an order fails to submit."""


def _extract_oid(result: dict) -> int | None:
    """Extract the order ID from an exchange response."""
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    if not statuses:
        return None
    status = statuses[0]
    if "resting" in status:
        return status["resting"]["oid"]
    if "filled" in status:
        return status["filled"]["oid"]
    return None


def _extract_fill(result: dict) -> dict | None:
    """Extract fill info from an exchange response."""
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    if not statuses:
        return None
    status = statuses[0]
    if "filled" in status:
        return status["filled"]
    return None


def _get_error(result: dict) -> str | None:
    """Extract error message from an exchange response."""
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    if not statuses:
        return None
    status = statuses[0]
    return status.get("error")


class PositionManager:
    """Manages trade lifecycle on the exchange, backed by database state.

    Each instance is scoped to a single user via the client and database.
    """

    def __init__(self, client: HyperliquidClient, db: TradeDatabase):
        self._client = client
        self._db = db

    # ------------------------------------------------------------------
    # Startup sync
    # ------------------------------------------------------------------

    def sync_positions(self) -> dict[str, list]:
        """Reconcile local DB state with actual exchange state.

        Call this once on startup to handle anything that changed while
        the bot was offline (fills, liquidations, expired orders, etc.).

        Returns a summary dict with lists of trade_ids that were updated:
            {
                "closed": [...],    # OPEN in DB, no position on exchange
                "canceled": [...],  # PENDING in DB, no resting entry order
                "verified": [...],  # OPEN in DB, position confirmed on exchange
                "orphans": [...],   # Positions on exchange with no local trade
            }
        """
        summary: dict[str, list] = {
            "closed": [],
            "canceled": [],
            "verified": [],
            "orphans": [],
        }

        # Fetch exchange state
        try:
            exchange_positions = self._client.get_open_positions()
            exchange_orders = self._client.get_open_orders()
        except Exception as e:
            logger.error("Failed to fetch exchange state for sync: %s", e)
            return summary

        # Index by coin for quick lookup
        positions_by_coin: dict[str, dict] = {
            p["coin"]: p for p in exchange_positions
        }
        # Collect all resting order oids
        resting_oids: set[int] = {
            int(o["oid"]) for o in exchange_orders if "oid" in o
        }

        # Load local open/pending trades
        local_trades = self._db.get_open_trades()
        local_coins: set[str] = set()

        for trade in local_trades:
            local_coins.add(trade.coin)

            if trade.status == TradeStatus.OPEN:
                # Trade was OPEN — check if position still exists
                if trade.coin in positions_by_coin:
                    logger.info(
                        "Sync: trade #%d %s confirmed open (size=%s)",
                        trade.trade_id, trade.coin,
                        positions_by_coin[trade.coin]["size"],
                    )
                    summary["verified"].append(trade.trade_id)
                else:
                    # Position gone — SL/TP filled or liquidated while offline
                    logger.warning(
                        "Sync: trade #%d %s was OPEN but no position found — marking CLOSED",
                        trade.trade_id, trade.coin,
                    )
                    self._db.update_trade_status(
                        trade.trade_id, TradeStatus.CLOSED,
                        close_reason="sync_no_position",
                    )
                    summary["closed"].append(trade.trade_id)

            elif trade.status == TradeStatus.PENDING:
                # Trade was PENDING — check if entry order is still resting
                orders = self._db.get_orders_for_trade(trade.trade_id)
                entry_order = next(
                    (o for o in orders if o.order_type == OrderType.ENTRY),
                    None,
                )

                if entry_order and entry_order.oid and entry_order.oid in resting_oids:
                    # Entry still resting, trade is still pending
                    logger.info(
                        "Sync: trade #%d %s entry order still resting (oid=%d)",
                        trade.trade_id, trade.coin, entry_order.oid,
                    )
                    summary["verified"].append(trade.trade_id)
                elif trade.coin in positions_by_coin:
                    # Entry filled while offline — promote to OPEN
                    logger.info(
                        "Sync: trade #%d %s entry filled while offline — marking OPEN",
                        trade.trade_id, trade.coin,
                    )
                    self._db.update_trade_status(trade.trade_id, TradeStatus.OPEN)
                    summary["verified"].append(trade.trade_id)
                else:
                    # No resting order and no position — entry expired/rejected
                    logger.warning(
                        "Sync: trade #%d %s was PENDING but no order or position — marking CANCELED",
                        trade.trade_id, trade.coin,
                    )
                    self._db.update_trade_status(
                        trade.trade_id, TradeStatus.CANCELED,
                        close_reason="sync_no_order",
                    )
                    summary["canceled"].append(trade.trade_id)

        # Check for orphan positions (on exchange but no local trade)
        for coin, pos in positions_by_coin.items():
            if coin not in local_coins:
                logger.warning(
                    "Sync: orphan position found — %s size=%s (no matching local trade)",
                    coin, pos["size"],
                )
                summary["orphans"].append(coin)

        logger.info(
            "Sync complete: %d verified, %d closed, %d canceled, %d orphans",
            len(summary["verified"]),
            len(summary["closed"]),
            len(summary["canceled"]),
            len(summary["orphans"]),
        )
        return summary

    # ------------------------------------------------------------------
    # Trade submission
    # ------------------------------------------------------------------

    def submit_trade(self, trade_set: TradeOrderSet) -> bool:
        """Submit a complete trade (entry + SL + TPs) to the exchange.

        Sets leverage, submits the entry order, then submits SL and TP
        orders. All orders are recorded in the database.

        Args:
            trade_set: The complete order set from build_orders().

        Returns:
            True if the entry order was accepted (resting or filled).
            False if the entry was rejected.
        """
        trade_id = trade_set.trade_id
        coin = trade_set.coin

        # --- Set leverage ---
        try:
            self._client.exchange.update_leverage(
                trade_set.leverage, coin, is_cross=trade_set.is_cross
            )
            logger.info("Set leverage: %s %dx cross=%s", coin, trade_set.leverage, trade_set.is_cross)
        except Exception as e:
            logger.error("Failed to set leverage for %s: %s", coin, e)
            return False

        # --- Submit entry order ---
        entry_row_id = self._db.record_order(
            trade_id, OrderType.ENTRY, coin,
            "BUY" if trade_set.entry.is_buy else "SELL",
            trade_set.entry.sz, trade_set.entry.limit_px,
        )

        entry_result = self._submit_order(trade_set.entry)
        error = _get_error(entry_result)
        if error:
            logger.error("Entry order rejected for #%d: %s", trade_id, error)
            self._db.update_order_status(entry_row_id, OrderStatus.REJECTED)
            return False

        entry_oid = _extract_oid(entry_result)
        if entry_oid:
            self._db.set_order_oid(entry_row_id, entry_oid)

        fill = _extract_fill(entry_result)
        if fill:
            self._db.update_order_status(entry_oid, OrderStatus.FILLED, float(fill.get("avgPx", 0)))
            self._db.update_trade_status(trade_id, TradeStatus.OPEN)
            logger.info("Entry filled immediately: #%d %s @ %s", trade_id, coin, fill.get("avgPx"))
        else:
            logger.info("Entry resting: #%d %s oid=%s", trade_id, coin, entry_oid)

        # --- Submit SL order ---
        self._submit_and_record(
            trade_id, OrderType.STOP_LOSS, trade_set.stop_loss, coin
        )

        # --- Submit TP orders ---
        tp_types = [OrderType.TP1, OrderType.TP2, OrderType.TP3]
        for tp_type, tp_order in zip(tp_types, trade_set.take_profits):
            if tp_order.sz > 0:
                self._submit_and_record(trade_id, tp_type, tp_order, coin)

        return True

    def cancel_trade(self, trade_id: int) -> None:
        """Cancel all open orders for a trade on the exchange."""
        orders = self._db.get_orders_for_trade(trade_id)
        for order in orders:
            if order.status in (OrderStatus.SUBMITTED,) and order.oid:
                try:
                    self._client.exchange.cancel(order.coin, order.oid)
                    self._db.update_order_status(order.oid, OrderStatus.CANCELED)
                    logger.info("Canceled order oid=%d for trade #%d", order.oid, trade_id)
                except Exception as e:
                    logger.error("Failed to cancel oid=%d: %s", order.oid, e)

        self._db.update_trade_status(trade_id, TradeStatus.CANCELED, close_reason="canceled")

    def close_position(self, trade_id: int, coin: str, reason: str = "manual") -> None:
        """Market-close any remaining position for a trade.

        Cancels open orders first, then submits a market close if a position exists.
        """
        # Cancel any remaining open orders
        self.cancel_trade(trade_id)

        # Check if there's still an open position
        positions = self._client.get_open_positions()
        pos = next((p for p in positions if p["coin"] == coin), None)
        if not pos or pos["size"] == 0:
            self._db.update_trade_status(trade_id, TradeStatus.CLOSED, close_reason=reason)
            return

        # Market close: sell if long, buy if short
        size = abs(pos["size"])
        is_buy = pos["size"] < 0  # buy to close short, sell to close long
        # Use aggressive price for IOC
        mids = self._client.get_all_mids()
        mid = float(mids.get(coin, 0))
        # Set limit far from mid to ensure fill
        limit_px = mid * 0.9 if not is_buy else mid * 1.1

        result = self._client.exchange.order(
            coin, is_buy, size, limit_px,
            {"limit": {"tif": "Ioc"}},
            reduce_only=True,
        )

        fill = _extract_fill(result)
        if fill:
            logger.info("Position closed: %s %s @ %s", coin, fill.get("totalSz"), fill.get("avgPx"))
        else:
            logger.warning("Position close may not have filled fully: %s", result)

        self._db.update_trade_status(trade_id, TradeStatus.CLOSED, close_reason=reason)

    def move_stop_loss(self, trade_id: int, coin: str, new_price: float) -> bool:
        """Cancel the existing SL and place a new one at *new_price*.

        Returns True if the new SL was placed, False otherwise.
        """
        orders = self._db.get_orders_for_trade(trade_id)
        sl_order = next(
            (o for o in orders if o.order_type == OrderType.STOP_LOSS and o.status == OrderStatus.SUBMITTED),
            None,
        )
        if not sl_order or not sl_order.oid:
            logger.warning("No active SL found for trade #%d to move", trade_id)
            return False

        # Cancel old SL
        try:
            self._client.exchange.cancel(coin, sl_order.oid)
            self._db.update_order_status(sl_order.oid, OrderStatus.CANCELED)
            logger.info("Canceled old SL oid=%d for trade #%d", sl_order.oid, trade_id)
        except Exception as e:
            logger.error("Failed to cancel old SL oid=%d: %s", sl_order.oid, e)
            return False

        # Determine direction: if original SL was a BUY (closing a short), new one is also BUY
        is_buy = sl_order.side == "BUY"

        # Place new SL at the requested price
        new_sl = OrderParams(
            coin=coin,
            is_buy=is_buy,
            sz=sl_order.size,
            limit_px=new_price,
            order_type={
                "trigger": {
                    "triggerPx": new_price,
                    "isMarket": True,
                    "tpsl": "sl",
                }
            },
            reduce_only=True,
        )
        oid = self._submit_and_record(trade_id, OrderType.STOP_LOSS, new_sl, coin)
        logger.info("Moved SL to %.6f for trade #%d", new_price, trade_id)
        return oid is not None

    def move_sl_to_breakeven(self, trade_id: int, coin: str, entry_price: float) -> bool:
        """Convenience wrapper — move SL to the entry price."""
        return self.move_stop_loss(trade_id, coin, entry_price)

    def _submit_order(self, params: OrderParams) -> dict:
        """Submit a single order to the exchange."""
        req = order_params_to_sdk_request(params)
        return self._client.exchange.order(
            req["coin"], req["is_buy"], req["sz"], req["limit_px"],
            req["order_type"], reduce_only=req["reduce_only"],
        )

    def _submit_and_record(
        self, trade_id: int, order_type: OrderType, params: OrderParams, coin: str
    ) -> int | None:
        """Submit an order and record it in the database. Returns the oid or None."""
        row_id = self._db.record_order(
            trade_id, order_type, coin,
            "BUY" if params.is_buy else "SELL",
            params.sz, params.limit_px,
        )

        result = self._submit_order(params)
        error = _get_error(result)
        if error:
            logger.error("Order %s rejected for #%d: %s", order_type.value, trade_id, error)
            self._db.update_order_status(row_id, OrderStatus.REJECTED)
            return None

        oid = _extract_oid(result)
        if oid:
            self._db.set_order_oid(row_id, oid)

        fill = _extract_fill(result)
        if fill and oid:
            self._db.update_order_status(oid, OrderStatus.FILLED, float(fill.get("avgPx", 0)))

        return oid
