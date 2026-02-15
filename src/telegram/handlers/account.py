"""Account monitoring handlers — /balance, /positions, /status, /activate, /deactivate."""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from src.orchestrator import Orchestrator
from src.state.user_db import UserDatabase
from src.telegram.formatters import format_balance, format_positions, format_status
from src.telegram.keyboards import account_nav_keyboard
from src.telegram.middleware import registered_only

logger = logging.getLogger(__name__)


def _get_client(context: ContextTypes.DEFAULT_TYPE, user_id: str):
    """Get the HyperliquidClient for a registered user from the orchestrator."""
    orchestrator: Orchestrator | None = context.bot_data.get("orchestrator")
    if not orchestrator:
        return None
    ctx = orchestrator.pipelines.get(user_id)
    if not ctx:
        return None
    return ctx.client


@registered_only
async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /balance — show account balance."""
    user_id = context.user_data["user_id"]
    client = _get_client(context, user_id)

    if not client:
        await update.message.reply_text(
            "Your trading pipeline is not active. Use /activate or contact admin."
        )
        return

    try:
        balance = client.get_balance()
    except Exception as e:
        logger.error("Failed to fetch balance for user %s: %s", user_id, e)
        await update.message.reply_text("Failed to fetch balance. Try again later.")
        return

    text = format_balance(balance)
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=account_nav_keyboard("balance"),
    )


@registered_only
async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /positions — show open positions."""
    user_id = context.user_data["user_id"]
    client = _get_client(context, user_id)

    if not client:
        await update.message.reply_text(
            "Your trading pipeline is not active. Use /activate or contact admin."
        )
        return

    try:
        positions = client.get_open_positions()
    except Exception as e:
        logger.error("Failed to fetch positions for user %s: %s", user_id, e)
        await update.message.reply_text("Failed to fetch positions. Try again later.")
        return

    text = format_positions(positions)
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=account_nav_keyboard("positions"),
    )


@registered_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status — show risk dashboard and access info."""
    user_id = context.user_data["user_id"]
    user_db: UserDatabase = context.bot_data["user_db"]
    client = _get_client(context, user_id)

    if not client:
        await update.message.reply_text(
            "Your trading pipeline is not active. Use /activate or contact admin."
        )
        return

    user_config = user_db.get_user_config(user_id)
    expires_at = user_db.get_access_expiry(user_id)

    try:
        balance = client.get_balance()
        positions = client.get_open_positions()
    except Exception as e:
        logger.error("Failed to fetch account data for user %s: %s", user_id, e)
        await update.message.reply_text("Failed to fetch account data. Try again later.")
        return

    text = format_status(user_config, balance, positions, expires_at)
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=account_nav_keyboard("status"),
    )


async def account_nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard navigation between account views."""
    query = update.callback_query
    await query.answer()

    # Check registration (can't use decorator on callback queries easily)
    user_db: UserDatabase = context.bot_data["user_db"]
    chat_id = update.effective_chat.id
    user_id = user_db.get_user_by_telegram_chat_id(chat_id)
    if not user_id:
        await query.edit_message_text("You're not registered. Use /register to get started.")
        return

    client = _get_client(context, user_id)
    if not client:
        await query.edit_message_text("Your trading pipeline is not active.")
        return

    view = query.data.replace("nav:", "")

    try:
        if view == "balance":
            balance = client.get_balance()
            text = format_balance(balance)
        elif view == "positions":
            positions = client.get_open_positions()
            text = format_positions(positions)
        elif view == "status":
            user_config = user_db.get_user_config(user_id)
            expires_at = user_db.get_access_expiry(user_id)
            balance = client.get_balance()
            positions = client.get_open_positions()
            text = format_status(user_config, balance, positions, expires_at)
        else:
            return
    except Exception as e:
        logger.error("Failed to fetch data for nav:%s user %s: %s", view, user_id, e)
        await query.edit_message_text("Failed to fetch data. Try again later.")
        return

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=account_nav_keyboard(view),
    )


@registered_only
async def activate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /activate — resume receiving trade signals."""
    user_id = context.user_data["user_id"]
    orchestrator: Orchestrator | None = context.bot_data.get("orchestrator")

    if not orchestrator:
        await update.message.reply_text("Trading system is not available.")
        return

    paused = orchestrator.is_user_paused(user_id)
    if paused is None:
        await update.message.reply_text(
            "Your trading pipeline is not active. Contact admin."
        )
        return

    if not paused:
        await update.message.reply_text("Trading is already active.")
        return

    orchestrator.resume_user(user_id)
    await update.message.reply_text(
        "Trading activated. You will now receive trade signals."
    )


@registered_only
async def deactivate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /deactivate — pause receiving trade signals."""
    user_id = context.user_data["user_id"]
    orchestrator: Orchestrator | None = context.bot_data.get("orchestrator")

    if not orchestrator:
        await update.message.reply_text("Trading system is not available.")
        return

    paused = orchestrator.is_user_paused(user_id)
    if paused is None:
        await update.message.reply_text(
            "Your trading pipeline is not active. Contact admin."
        )
        return

    if paused:
        await update.message.reply_text("Trading is already paused.")
        return

    orchestrator.pause_user(user_id)
    await update.message.reply_text(
        "Trading deactivated. You will not receive new trade signals.\n"
        "Use /activate to resume."
    )
