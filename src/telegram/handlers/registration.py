"""Registration flow — multi-step ConversationHandler.

States: INVITE_CODE → ACCOUNT_ADDRESS → API_WALLET → API_SECRET → NETWORK

Each credential message is deleted immediately after reading.
DM-only check rejects registration in group chats.
"""

import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from src.exchange.hyperliquid import HyperliquidClient
from src.state.user_db import UserDatabase
from src.telegram.formatters import format_expiry, mask_address

logger = logging.getLogger(__name__)

# Conversation states
INVITE_CODE, ACCOUNT_ADDRESS, API_WALLET, API_SECRET, NETWORK = range(5)

# Regex for 0x-prefixed hex addresses/keys
_HEX_PATTERN = re.compile(r"^0x[0-9a-fA-F]+$")


def _get_user_db(context: ContextTypes.DEFAULT_TYPE) -> UserDatabase:
    return context.bot_data["user_db"]


def _get_orchestrator(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data.get("orchestrator")


async def register_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /register — start the registration flow."""
    # DM-only check
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Registration must be done in a private chat. Send me a DM."
        )
        return ConversationHandler.END

    user_db = _get_user_db(context)
    chat_id = update.effective_chat.id

    # Check if already registered
    existing_user = user_db.get_user_by_telegram_chat_id(chat_id)
    if existing_user:
        await update.message.reply_text("You're already registered.")
        return ConversationHandler.END

    await update.message.reply_text("Enter your invite code:")
    return INVITE_CODE


async def receive_invite_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate the invite code and proceed to credential collection."""
    user_db = _get_user_db(context)
    code = update.message.text.strip().upper()

    result = user_db.validate_invite_code(code)
    if not result["valid"]:
        reason = result["reason"]
        if "redeemed" in reason.lower():
            await update.message.reply_text(
                "This code has already been used. Contact admin for a new code."
            )
        else:
            await update.message.reply_text(
                "Invalid or expired invite code. Contact admin for access."
            )
        return ConversationHandler.END

    # Store validated code in user_data for later
    duration = result.get("duration_days")
    duration_text = f"{duration} days" if duration else "unlimited"
    context.user_data["invite_code"] = code
    context.user_data["duration_days"] = duration

    await update.message.reply_text(
        f"Code accepted! Access: {duration_text}\n\n"
        "Now I'll need your Hyperliquid API credentials.\n"
        "Make sure you're in a private chat.\n\n"
        "Send your *Account Address* (0x...):",
        parse_mode="Markdown",
    )
    return ACCOUNT_ADDRESS


async def receive_account_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive and validate the account address."""
    text = update.message.text.strip()

    # Delete the credential message
    try:
        await update.message.delete()
    except Exception:
        pass  # May fail if bot lacks delete permission

    if not _HEX_PATTERN.match(text) or len(text) != 42:
        await update.effective_chat.send_message(
            "Invalid address format. Must be a 42-character 0x-prefixed hex address.\n"
            "Send your *Account Address* (0x...):",
            parse_mode="Markdown",
        )
        return ACCOUNT_ADDRESS

    context.user_data["account_address"] = text
    masked = mask_address(text)
    await update.effective_chat.send_message(
        f"Account Address saved ({masked}).\n\n"
        "Now send your *API Wallet Address* (0x...):",
        parse_mode="Markdown",
    )
    return API_WALLET


async def receive_api_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive and validate the API wallet address."""
    text = update.message.text.strip()

    try:
        await update.message.delete()
    except Exception:
        pass

    if not _HEX_PATTERN.match(text) or len(text) != 42:
        await update.effective_chat.send_message(
            "Invalid address format. Must be a 42-character 0x-prefixed hex address.\n"
            "Send your *API Wallet Address* (0x...):",
            parse_mode="Markdown",
        )
        return API_WALLET

    context.user_data["api_wallet"] = text
    masked = mask_address(text)
    await update.effective_chat.send_message(
        f"API Wallet saved ({masked}).\n\n"
        "Now send your *API Private Key* (0x...):",
        parse_mode="Markdown",
    )
    return API_SECRET


async def receive_api_secret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the API private key and prompt for network selection."""
    text = update.message.text.strip()

    try:
        await update.message.delete()
    except Exception:
        pass

    if not _HEX_PATTERN.match(text) or len(text) != 66:
        await update.effective_chat.send_message(
            "Invalid private key format. Must be a 66-character 0x-prefixed hex string.\n"
            "Send your *API Private Key* (0x...):",
            parse_mode="Markdown",
        )
        return API_SECRET

    context.user_data["api_secret"] = text

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Testnet", callback_data="network:testnet"),
            InlineKeyboardButton("Mainnet", callback_data="network:mainnet"),
        ]
    ])
    await update.effective_chat.send_message(
        "Private Key encrypted.\n\nSelect network:",
        reply_markup=keyboard,
    )
    return NETWORK


async def receive_network(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle network selection, validate credentials, complete registration."""
    query = update.callback_query
    await query.answer()

    network = query.data.replace("network:", "")
    if network not in ("testnet", "mainnet"):
        await query.edit_message_text("Invalid network. Please select Testnet or Mainnet.")
        return NETWORK

    if network == "mainnet":
        await query.edit_message_text(
            "Only *Testnet* trading is available at the moment.\n\n"
            "Please select Testnet to continue.",
            parse_mode="Markdown",
        )
        return NETWORK

    context.user_data["network"] = network
    await query.edit_message_text(f"Network: {network}\n\nValidating credentials...")

    user_db = _get_user_db(context)
    chat_id = update.effective_chat.id

    account_address = context.user_data["account_address"]
    api_wallet = context.user_data["api_wallet"]
    api_secret = context.user_data["api_secret"]

    # Validate credentials against Hyperliquid
    try:
        client = HyperliquidClient(
            account_address=account_address,
            private_key=api_secret,
            network=network,
        )
        client.get_account_state()
    except Exception as e:
        logger.warning("Credential validation failed for chat %d: %s", chat_id, e)
        await query.edit_message_text(
            f"Credential validation failed: {e}\n\n"
            "Please check your credentials and try /register again."
        )
        _clear_user_data(context)
        return ConversationHandler.END

    # Create user in DB
    user_id = str(chat_id)
    try:
        user_db.create_user(
            user_id=user_id,
            display_name=update.effective_user.full_name or user_id,
            credentials={
                "account_address": account_address,
                "api_wallet": api_wallet,
                "api_secret": api_secret,
                "network": network,
            },
        )
    except Exception as e:
        logger.error("Failed to create user %s: %s", user_id, e)
        await query.edit_message_text(
            "Registration failed. You may already be registered. Contact admin."
        )
        _clear_user_data(context)
        return ConversationHandler.END

    # Store telegram chat ID
    user_db.set_telegram_chat_id(user_id, chat_id)

    # Redeem invite code
    invite_code = context.user_data["invite_code"]
    expires_at = user_db.redeem_invite_code(invite_code, user_id)

    # Activate pipeline via orchestrator
    orchestrator = _get_orchestrator(context)
    if orchestrator:
        try:
            orchestrator.activate_user(user_id)
        except Exception as e:
            logger.error("Failed to activate pipeline for user %s: %s", user_id, e)

    # Build completion message
    expiry_text = format_expiry(expires_at)
    await query.edit_message_text(
        f"*Registration complete!*\n\n"
        f"Access expires: {expiry_text}\n"
        f"Strategy: runner (33/33/34)\n"
        f"Auto-execute: OFF\n"
        f"Max leverage: 20x\n\n"
        f"Use /help to see all commands.",
        parse_mode="Markdown",
    )

    _clear_user_data(context)
    logger.info("User %s registered successfully (network=%s)", user_id, network)
    return ConversationHandler.END


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cancel — exit registration at any step."""
    _clear_user_data(context)
    await update.message.reply_text("Registration cancelled.")
    return ConversationHandler.END


def _clear_user_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove sensitive data from user_data."""
    for key in ("invite_code", "duration_days", "account_address", "api_wallet", "api_secret", "network"):
        context.user_data.pop(key, None)


def build_registration_handler() -> ConversationHandler:
    """Build the ConversationHandler for the registration flow."""
    return ConversationHandler(
        entry_points=[CommandHandler("register", register_command)],
        states={
            INVITE_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_invite_code)],
            ACCOUNT_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_account_address)],
            API_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_wallet)],
            API_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_secret)],
            NETWORK: [CallbackQueryHandler(receive_network, pattern=r"^network:")],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )
