"""Trade approval handlers — Approve/Reject/Close Position callbacks.

Wires up the inline buttons from push notifications (Step 8) so users can:
- Approve a pending trade → reconstructs orders and submits to exchange
- Reject a pending trade → cancels it
- Close an open trade → market-closes the position (with confirmation)
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.exchange.order_builder import build_orders
from src.exchange.position_manager import OrderSubmissionError, PositionManager
from src.orchestrator import Orchestrator
from src.parser.signal_parser import ParsedSignal, RiskLevel, Side
from src.state.models import TradeRecord, TradeStatus
from src.state.user_db import UserDatabase
from src.telegram.handlers.trades import _format_trade_detail

logger = logging.getLogger(__name__)


def _reconstruct_signal(trade: TradeRecord) -> ParsedSignal:
    """Rebuild a ParsedSignal from a stored TradeRecord."""
    return ParsedSignal(
        pair=trade.pair,
        trade_id=trade.trade_id,
        risk_level=RiskLevel(trade.risk_level),
        trade_type=trade.trade_type,
        size=trade.size_hint,
        side=Side(trade.side),
        entry=trade.entry_price,
        stop_loss=trade.stop_loss,
        tp1=trade.tp1,
        tp2=trade.tp2,
        tp3=trade.tp3,
        leverage=trade.signal_leverage,
    )


async def signal_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle signal:approve:{id} and signal:reject:{id} callbacks."""
    query = update.callback_query
    await query.answer()

    data = query.data
    parts = data.split(":")
    action = parts[1]  # "approve" or "reject"
    trade_id = int(parts[2])

    # Look up user
    user_db: UserDatabase = context.bot_data["user_db"]
    chat_id = update.effective_chat.id
    user_id = user_db.get_user_by_telegram_chat_id(chat_id)
    if not user_id:
        await query.edit_message_text("You're not registered.")
        return

    # Get pipeline context
    orchestrator: Orchestrator | None = context.bot_data.get("orchestrator")
    if not orchestrator:
        await query.edit_message_text("Trading system is not active.")
        return

    ctx = orchestrator.pipelines.get(user_id)
    if not ctx:
        await query.edit_message_text("Your trading pipeline is not active.")
        return

    # Fetch trade and validate
    trade = ctx.db.get_trade(trade_id)
    if not trade:
        await query.edit_message_text(f"Trade #{trade_id} not found.")
        return

    if trade.status != TradeStatus.PENDING:
        await query.edit_message_text(
            f"Trade #{trade_id} is no longer pending (status: {trade.status.value})."
        )
        return

    if action == "reject":
        ctx.db.update_trade_status(trade_id, TradeStatus.CANCELED, close_reason="rejected")
        await query.edit_message_text(
            f"*Rejected* — Trade #{trade_id} canceled.",
            parse_mode="Markdown",
        )
        return

    # action == "approve"
    try:
        signal = _reconstruct_signal(trade)
        trade_set = build_orders(
            signal,
            trade.position_size_usd,
            ctx.pipeline._asset_meta,
            tp_split=ctx.config.get_active_preset().tp_split,
            max_leverage=ctx.config.strategy.max_leverage,
        )

        pm = PositionManager(ctx.client, ctx.db)
        pm.submit_trade(trade_set)

        await query.edit_message_text(
            f"*Approved* — Trade #{trade_id} submitted to exchange.",
            parse_mode="Markdown",
        )
    except OrderSubmissionError as e:
        logger.error("Trade #%d submission failed: %s", trade_id, e)
        ctx.db.update_trade_status(trade_id, TradeStatus.CANCELED, close_reason="submission_failed")
        await query.edit_message_text(
            f"*Approval failed* — {e}\nTrade #{trade_id} canceled.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception("Error approving trade #%d", trade_id)
        ctx.db.update_trade_status(trade_id, TradeStatus.CANCELED, close_reason="approval_error")
        await query.edit_message_text(
            f"*Approval failed* — {e}\nTrade #{trade_id} canceled.",
            parse_mode="Markdown",
        )


async def close_trade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle close_trade:{id} — show confirmation dialog."""
    query = update.callback_query
    await query.answer()

    trade_id = int(query.data.split(":")[1])

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Yes, close it", callback_data=f"confirm_close:{trade_id}"),
            InlineKeyboardButton("Cancel", callback_data=f"cancel_close:{trade_id}"),
        ]
    ])

    await query.edit_message_text(
        f"*Close Trade #{trade_id}?*\n\nThis will market-close your position.",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def confirm_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle confirm_close:{id} and cancel_close:{id} callbacks."""
    query = update.callback_query
    await query.answer()

    data = query.data
    parts = data.split(":")
    action = parts[0]  # "confirm_close" or "cancel_close"
    trade_id = int(parts[1])

    # Look up user
    user_db: UserDatabase = context.bot_data["user_db"]
    chat_id = update.effective_chat.id
    user_id = user_db.get_user_by_telegram_chat_id(chat_id)
    if not user_id:
        await query.edit_message_text("You're not registered.")
        return

    # Get pipeline context
    orchestrator: Orchestrator | None = context.bot_data.get("orchestrator")
    if not orchestrator:
        await query.edit_message_text("Trading system is not active.")
        return

    ctx = orchestrator.pipelines.get(user_id)
    if not ctx:
        await query.edit_message_text("Your trading pipeline is not active.")
        return

    trade = ctx.db.get_trade(trade_id)
    if not trade:
        await query.edit_message_text(f"Trade #{trade_id} not found.")
        return

    if action == "cancel_close":
        # Return to trade detail view
        text = _format_trade_detail(trade)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Close Position", callback_data=f"close_trade:{trade_id}")],
            [InlineKeyboardButton("< Back to Trades", callback_data="back:trades")],
        ])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return

    # action == "confirm_close"
    if trade.status not in (TradeStatus.OPEN, TradeStatus.PENDING):
        await query.edit_message_text(
            f"Trade #{trade_id} is not open or pending (status: {trade.status.value})."
        )
        return

    try:
        pm = PositionManager(ctx.client, ctx.db)
        if trade.status == TradeStatus.PENDING:
            pm.cancel_trade(trade_id)
            await query.edit_message_text(
                f"*Trade Canceled* — Trade #{trade_id} ({trade.coin}) orders canceled.",
                parse_mode="Markdown",
            )
        else:
            pm.close_position(trade_id, trade.coin, reason="manual_telegram")
            await query.edit_message_text(
                f"*Position Closed* — Trade #{trade_id} ({trade.coin}) has been market-closed.",
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.exception("Error closing trade #%d", trade_id)
        await query.edit_message_text(
            f"*Close failed* — {e}",
            parse_mode="Markdown",
        )
