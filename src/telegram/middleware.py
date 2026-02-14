"""Telegram bot middleware — auth checks, admin validation, DM enforcement."""

import logging
from datetime import datetime, timezone
from functools import wraps
from typing import Callable

from telegram import Update
from telegram.ext import ContextTypes

from src.state.user_db import UserDatabase

logger = logging.getLogger(__name__)


def admin_only(func: Callable) -> Callable:
    """Decorator that restricts a handler to admin users only.

    Checks the caller's Telegram user ID against the TELEGRAM_ADMIN_IDS
    stored in context.bot_data["admin_ids"].
    """
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        admin_ids: list[int] = context.bot_data.get("admin_ids", [])
        user_id = update.effective_user.id

        if user_id not in admin_ids:
            logger.warning("Non-admin %d attempted admin command %s", user_id, func.__name__)
            await update.message.reply_text("This command is only available to administrators.")
            return

        return await func(update, context)

    return wrapper


def registered_only(func: Callable) -> Callable:
    """Decorator that restricts a handler to registered users only.

    Looks up the caller's Telegram chat ID in the user database.
    Stores the resolved user_id in context.user_data["user_id"] for the handler.
    """
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_db: UserDatabase = context.bot_data["user_db"]
        chat_id = update.effective_chat.id
        user_id = user_db.get_user_by_telegram_chat_id(chat_id)

        if not user_id:
            await update.message.reply_text(
                "You're not registered. Use /register to get started."
            )
            return

        # Check access expiry
        expiry = user_db.get_access_expiry(user_id)
        if expiry is not None:
            expiry_dt = datetime.fromisoformat(expiry)
            if expiry_dt <= datetime.now(timezone.utc):
                await update.message.reply_text(
                    "Your access has expired. Contact admin to renew."
                )
                return

        context.user_data["user_id"] = user_id
        return await func(update, context)

    return wrapper
