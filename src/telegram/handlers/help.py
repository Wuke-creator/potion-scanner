"""Help and start command handlers."""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.state.user_db import UserDatabase

logger = logging.getLogger(__name__)

WELCOME_MESSAGE = (
    "🧪 *Welcome to Potion Perps Bot!*\n\n"
    "Automated Hyperliquid perpetual futures trading, "
    "powered by Potion Perps signals.\n\n"
    "To get started, you'll need an invite code from an admin."
)

HELP_MESSAGE = (
    "📚 *Available Commands*\n\n"
    "🏠 *Navigation*\n"
    "/menu — Open main menu\n"
    "/start — Welcome / main menu\n"
    "/help — Show this help message\n\n"
    "🔐 *Getting Started*\n"
    "/register — Register with an invite code\n\n"
    "📊 *Trading Shortcuts*\n"
    "/balance — Account balance\n"
    "/positions — Open positions\n"
    "/trades — Active trades\n"
    "/history — Trade history\n"
    "/stats — Trading statistics\n"
    "/status — Risk dashboard\n\n"
    "⚙️ *Config Shortcuts*\n"
    "/config — View & change settings\n"
    "/preset — Change strategy preset\n"
    "/auto — Toggle auto-execute\n"
    "/activate — Resume signals\n"
    "/deactivate — Pause signals\n\n"
    "🛠 *Other*\n"
    "/cancel — Cancel current action\n"
    "/admin — Admin commands\n\n"
    "💡 _Tip: Use /menu for button navigation!_"
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — show main menu for registered users, welcome for others."""
    logger.info("User %s sent /start", update.effective_user.id)

    user_db: UserDatabase = context.bot_data["user_db"]
    chat_id = update.effective_chat.id
    user_id = user_db.get_user_by_telegram_chat_id(chat_id)

    if user_id:
        # Registered user → show main menu
        from src.telegram.handlers.menu import menu_command
        context.user_data["user_id"] = user_id
        await menu_command.__wrapped__(update, context)
    else:
        # Not registered → pre-registration welcome
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 Sign In / Register", callback_data="start:register")],
        ])
        await update.message.reply_text(
            WELCOME_MESSAGE,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )


async def start_register_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the Sign In / Register button — prompt user to use /register."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🔑 *Getting Started*\n\n"
        "Use /register to begin setup.\n"
        "You'll need an invite code from an admin.",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help — list available commands."""
    logger.info("User %s sent /help", update.effective_user.id)
    await update.message.reply_text(HELP_MESSAGE, parse_mode="Markdown")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel — reset any pending state."""
    if context.user_data:
        context.user_data.clear()
    await update.message.reply_text("❌ Action cancelled. Use /menu to open the menu.")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle unknown commands."""
    await update.message.reply_text(
        "❓ Unknown command. Type /help to see available commands."
    )
