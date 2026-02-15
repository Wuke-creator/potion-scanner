"""Telegram bot middleware — auth checks, admin validation, DM enforcement, rate limiting."""

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from functools import wraps
from typing import Callable

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from src.state.user_db import UserDatabase

logger = logging.getLogger(__name__)

# Rate limiting: per-user command timestamps
_rate_limit_window = 60  # seconds
_rate_limit_max = 30  # max commands per window
_user_timestamps: dict[int, list[float]] = defaultdict(list)


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


def check_rate_limit(user_id: int) -> bool:
    """Return True if the user is within rate limits, False if exceeded."""
    now = time.monotonic()
    timestamps = _user_timestamps[user_id]
    # Purge old timestamps
    cutoff = now - _rate_limit_window
    _user_timestamps[user_id] = [t for t in timestamps if t > cutoff]
    if len(_user_timestamps[user_id]) >= _rate_limit_max:
        return False
    _user_timestamps[user_id].append(now)
    return True


async def dm_only_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pre-handler check: ignore messages from group chats."""
    if not update.effective_chat or update.effective_chat.type == "private":
        return
    # Non-private chat — block
    if update.message:
        await update.message.reply_text(
            "I only work in private chats. Send me a DM!"
        )
    raise ApplicationHandlerStop()


async def rate_limit_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pre-handler check: enforce per-user rate limiting."""
    if not update.effective_user:
        return
    if check_rate_limit(update.effective_user.id):
        return
    # Rate limit exceeded
    if update.message:
        await update.message.reply_text(
            "You're sending commands too fast. Please wait a moment."
        )
    raise ApplicationHandlerStop()


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler — sends a friendly message for uncaught exceptions."""
    logger.exception("Unhandled error: %s", context.error)
    try:
        if hasattr(update, "message") and update.message:
            await update.message.reply_text(
                "Something went wrong. Please try again later."
            )
    except Exception:
        pass
