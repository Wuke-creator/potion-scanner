"""Help and start command handlers."""

import logging

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

WELCOME_MESSAGE = (
    "Welcome to *Potion Perps Bot*\n\n"
    "Automated Hyperliquid perpetual futures trading, "
    "powered by Potion Perps signals.\n\n"
    "To get started, you'll need an invite code from an admin.\n"
    "Use /register to begin setup.\n\n"
    "Type /help to see all available commands."
)

HELP_MESSAGE = (
    "*Available Commands*\n\n"
    "*Getting Started*\n"
    "/start — Welcome message\n"
    "/register — Register with an invite code\n"
    "/help — Show this help message\n\n"
    "*Account*\n"
    "/balance — Account balance\n"
    "/positions — Open positions\n"
    "/status — Risk dashboard & access info\n\n"
    "*Trading*\n"
    "/trades — Active trades\n"
    "/history — Trade history\n"
    "/stats — Trading statistics\n\n"
    "*Configuration*\n"
    "/config — View & change settings\n"
    "/preset — Change strategy preset\n"
    "/auto — Toggle auto-execute\n\n"
    "*Control*\n"
    "/activate — Activate trading\n"
    "/deactivate — Pause trading\n"
    "/cancel — Cancel current action\n\n"
    "*Admin*\n"
    "/admin — Admin commands (admin only)"
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — send welcome message."""
    logger.info("User %s sent /start", update.effective_user.id)
    await update.message.reply_text(WELCOME_MESSAGE, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help — list available commands."""
    logger.info("User %s sent /help", update.effective_user.id)
    await update.message.reply_text(HELP_MESSAGE, parse_mode="Markdown")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel — reset any pending state."""
    # Clear any user_data state that might be lingering
    if context.user_data:
        context.user_data.clear()
    await update.message.reply_text("Action cancelled. Use /help to see available commands.")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle unknown commands."""
    await update.message.reply_text(
        "Unknown command. Type /help to see available commands."
    )
