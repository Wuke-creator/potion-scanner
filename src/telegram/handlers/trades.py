"""Trade view handlers — /trades, /history, /stats, trading:* callbacks."""

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.orchestrator import Orchestrator
from src.state.models import TradeRecord, TradeStatus
from src.state.user_db import UserDatabase
from src.telegram.formatters import format_balance, format_positions, format_stats
from src.telegram.keyboards import trading_sub_keyboard
from src.telegram.middleware import registered_only

logger = logging.getLogger(__name__)

TRADES_PER_PAGE = 5


def _get_trade_db(context: ContextTypes.DEFAULT_TYPE, user_id: str):
    """Get the TradeDatabase for a user from the orchestrator."""
    orchestrator: Orchestrator | None = context.bot_data.get("orchestrator")
    if not orchestrator:
        return None
    ctx = orchestrator.pipelines.get(user_id)
    return ctx.db if ctx else None


def _get_client(context: ContextTypes.DEFAULT_TYPE, user_id: str):
    """Get the HyperliquidClient for a user from the orchestrator."""
    orchestrator: Orchestrator | None = context.bot_data.get("orchestrator")
    if not orchestrator:
        return None
    ctx = orchestrator.pipelines.get(user_id)
    return ctx.client if ctx else None


def _format_trade_summary(t: TradeRecord) -> str:
    """Format a single trade as a compact summary line."""
    status_icon = {
        "pending": "⏳",
        "open": "🟢",
        "closed": "✅",
        "canceled": "🚫",
        "preparing": "📝",
    }.get(t.status.value, "❓")

    side_icon = "📈" if t.side.upper() == "LONG" else "📉"

    pnl_text = ""
    if t.pnl_pct is not None:
        sign = "+" if t.pnl_pct >= 0 else ""
        pnl_text = f" | {sign}{t.pnl_pct:.1f}%"

    return (
        f"{status_icon} *#{t.trade_id}* {side_icon} {t.coin} {t.side}\n"
        f"  Entry: {t.entry_price} | SL: {t.stop_loss} | Lev: {t.leverage}x{pnl_text}"
    )


def _format_trade_detail(t: TradeRecord) -> str:
    """Format full trade detail view."""
    status_icon = {
        "pending": "⏳ Pending",
        "open": "🟢 Open",
        "closed": "✅ Closed",
        "canceled": "🚫 Canceled",
        "preparing": "📝 Preparing",
    }.get(t.status.value, t.status.value)

    side_icon = "📈" if t.side.upper() == "LONG" else "📉"

    lines = [
        f"{side_icon} *Trade #{t.trade_id} — {t.coin} {t.side}*\n",
        f"Status: {status_icon}",
        f"Pair: {t.pair}",
        f"Type: {t.trade_type} | Risk: {t.risk_level}",
        f"📊 Leverage: {t.leverage}x (signal: {t.signal_leverage}x)",
        f"💰 Size: ${t.position_size_usd:,.2f} ({t.position_size_coin} {t.coin})\n",
        "🎯 *Prices*",
        f"Entry: {t.entry_price}",
        f"🛡 Stop Loss: {t.stop_loss}",
        f"TP1: {t.tp1}",
        f"TP2: {t.tp2}",
        f"TP3: {t.tp3}",
    ]

    if t.pnl_pct is not None:
        sign = "+" if t.pnl_pct >= 0 else ""
        emoji = "💹" if t.pnl_pct >= 0 else "📉"
        lines.append(f"\n{emoji} PnL: {sign}{t.pnl_pct:.2f}%")

    if t.close_reason:
        lines.append(f"📋 Close Reason: {t.close_reason}")

    if t.created_at:
        lines.append(f"\n📅 Opened: {str(t.created_at)[:19]}")
    if t.closed_at:
        lines.append(f"📅 Closed: {str(t.closed_at)[:19]}")

    return "\n".join(lines)


def _pagination_keyboard(prefix: str, page: int, total_pages: int) -> InlineKeyboardMarkup | None:
    """Build pagination buttons if needed."""
    if total_pages <= 1:
        return None

    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{prefix}:{page - 1}"))
    buttons.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}:{page + 1}"))
    return InlineKeyboardMarkup([buttons])


def _format_trade_list(trades: list[TradeRecord], page: int, title: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Format a paginated list of trades."""
    if not trades:
        return f"📋 *{title}*\n\nNo trades found.", None

    total_pages = (len(trades) + TRADES_PER_PAGE - 1) // TRADES_PER_PAGE
    page = max(0, min(page, total_pages - 1))
    start = page * TRADES_PER_PAGE
    page_trades = trades[start:start + TRADES_PER_PAGE]

    lines = [f"📋 *{title}*\n"]
    for t in page_trades:
        lines.append(_format_trade_summary(t))

    # Add trade detail buttons
    detail_buttons = [
        InlineKeyboardButton(f"#{t.trade_id}", callback_data=f"trade:{t.trade_id}")
        for t in page_trades
    ]
    # Split into rows of 5
    keyboard_rows = [detail_buttons[i:i + 5] for i in range(0, len(detail_buttons), 5)]

    # Pagination row
    if total_pages > 1:
        nav_buttons = []
        prefix = "trades_page" if title == "Active Trades" else "history_page"
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{prefix}:{page - 1}"))
        nav_buttons.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}:{page + 1}"))
        keyboard_rows.append(nav_buttons)

    # Back to trading nav
    keyboard_rows.append([InlineKeyboardButton("⬅️ Trading", callback_data="menu:trading")])

    keyboard = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None
    return "\n\n".join(lines), keyboard


@registered_only
async def trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /trades — list active trades (pending + open)."""
    user_id = context.user_data["user_id"]
    trade_db = _get_trade_db(context, user_id)

    if not trade_db:
        await update.message.reply_text(
            "⚠️ Your trading pipeline is not active. Use /activate or contact admin."
        )
        return

    trades = trade_db.get_open_trades()
    text, keyboard = _format_trade_list(trades, 0, "Active Trades")
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


@registered_only
async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /history — list recently closed trades."""
    user_id = context.user_data["user_id"]
    trade_db = _get_trade_db(context, user_id)

    if not trade_db:
        await update.message.reply_text(
            "⚠️ Your trading pipeline is not active. Use /activate or contact admin."
        )
        return

    closed = trade_db.get_completed_trades()
    # Most recent first
    closed.sort(key=lambda t: t.closed_at or t.updated_at, reverse=True)
    text, keyboard = _format_trade_list(closed, 0, "Trade History")
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


@registered_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stats — trading performance summary."""
    user_id = context.user_data["user_id"]
    trade_db = _get_trade_db(context, user_id)

    if not trade_db:
        await update.message.reply_text(
            "⚠️ Your trading pipeline is not active. Use /activate or contact admin."
        )
        return

    closed = trade_db.get_trades_by_status(TradeStatus.CLOSED)
    open_trades = trade_db.get_open_trades()
    text = format_stats(closed, len(open_trades))

    from src.telegram.keyboards import stats_keyboard
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=stats_keyboard())


async def trade_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle trade detail view via inline button."""
    query = update.callback_query
    await query.answer()

    user_db: UserDatabase = context.bot_data["user_db"]
    chat_id = update.effective_chat.id
    user_id = user_db.get_user_by_telegram_chat_id(chat_id)
    if not user_id:
        await query.edit_message_text("❌ You're not registered.")
        return

    trade_db = _get_trade_db(context, user_id)
    if not trade_db:
        await query.edit_message_text("⚠️ Your trading pipeline is not active.")
        return

    data = query.data
    if data.startswith("trade:"):
        trade_id = int(data.split(":")[1])
        trade = trade_db.get_trade(trade_id)
        if not trade:
            await query.edit_message_text(f"❌ Trade #{trade_id} not found.")
            return

        text = _format_trade_detail(trade)
        keyboard_rows = []
        if trade.status == TradeStatus.OPEN:
            keyboard_rows.append(
                [InlineKeyboardButton("🔴 Close Position", callback_data=f"close_trade:{trade_id}")]
            )
        elif trade.status == TradeStatus.PENDING:
            keyboard_rows.append(
                [InlineKeyboardButton("🚫 Cancel Trade", callback_data=f"close_trade:{trade_id}")]
            )
        keyboard_rows.append(
            [InlineKeyboardButton("⬅️ Back to Trades", callback_data="back:trades")]
        )
        back_button = InlineKeyboardMarkup(keyboard_rows)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_button)

    elif data.startswith("trades_page:"):
        page = int(data.split(":")[1])
        trades = trade_db.get_open_trades()
        text, keyboard = _format_trade_list(trades, page, "Active Trades")
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

    elif data.startswith("history_page:"):
        page = int(data.split(":")[1])
        closed = trade_db.get_completed_trades()
        closed.sort(key=lambda t: t.closed_at or t.updated_at, reverse=True)
        text, keyboard = _format_trade_list(closed, page, "Trade History")
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

    elif data == "back:trades":
        trades = trade_db.get_open_trades()
        text, keyboard = _format_trade_list(trades, 0, "Active Trades")
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

    elif data == "noop":
        pass  # Page indicator button, do nothing


async def trading_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle trading:* callbacks for trading sub-views."""
    query = update.callback_query
    await query.answer()

    user_db: UserDatabase = context.bot_data["user_db"]
    chat_id = update.effective_chat.id
    user_id = user_db.get_user_by_telegram_chat_id(chat_id)
    if not user_id:
        await query.edit_message_text("❌ You're not registered.")
        return

    data = query.data
    client = _get_client(context, user_id)
    trade_db = _get_trade_db(context, user_id)

    if data == "trading:balance":
        if not client:
            await query.edit_message_text("⚠️ Your trading pipeline is not active.")
            return
        try:
            balance = client.get_balance()
            text = format_balance(balance)
        except Exception as e:
            logger.error("Failed to fetch balance for user %s: %s", user_id, e)
            text = "💰 *Account Balance*\n\n⚠️ Failed to fetch balance."
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=trading_sub_keyboard())

    elif data == "trading:positions":
        if not client:
            await query.edit_message_text("⚠️ Your trading pipeline is not active.")
            return
        try:
            positions = client.get_open_positions()
            text = format_positions(positions)
        except Exception as e:
            logger.error("Failed to fetch positions for user %s: %s", user_id, e)
            text = "📂 *Open Positions*\n\n⚠️ Failed to fetch positions."
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=trading_sub_keyboard())

    elif data == "trading:trades":
        if not trade_db:
            await query.edit_message_text("⚠️ Your trading pipeline is not active.")
            return
        trades = trade_db.get_open_trades()
        text, keyboard = _format_trade_list(trades, 0, "Active Trades")
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

    elif data == "trading:history":
        if not trade_db:
            await query.edit_message_text("⚠️ Your trading pipeline is not active.")
            return
        closed = trade_db.get_completed_trades()
        closed.sort(key=lambda t: t.closed_at or t.updated_at, reverse=True)
        text, keyboard = _format_trade_list(closed, 0, "Trade History")
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
