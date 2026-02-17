"""Central menu hub — main menu and all menu:* callback routing.

Every screen is one message with formatted text + inline keyboard buttons.
Screens update in-place via edit_message_text (no new messages).
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.orchestrator import Orchestrator
from src.state.models import TradeStatus
from src.state.user_db import UserDatabase
from src.telegram.formatters import (
    format_account_info,
    format_calls_view,
    format_dashboard,
    format_stats,
    format_trading_hub,
    mask_address,
)
from src.telegram.keyboards import (
    account_keyboard,
    calls_view_keyboard,
    config_menu_keyboard,
    dashboard_keyboard,
    main_menu_keyboard,
    stats_keyboard,
    trading_hub_keyboard,
)
from src.telegram.middleware import registered_only

logger = logging.getLogger(__name__)

# Number of recent signals to show in calls view
CALLS_LIMIT = 10


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _get_user_db(context: ContextTypes.DEFAULT_TYPE) -> UserDatabase:
    return context.bot_data["user_db"]


def _get_orchestrator(context: ContextTypes.DEFAULT_TYPE) -> Orchestrator | None:
    return context.bot_data.get("orchestrator")


def _get_client(context: ContextTypes.DEFAULT_TYPE, user_id: str):
    orchestrator = _get_orchestrator(context)
    if not orchestrator:
        return None
    ctx = orchestrator.pipelines.get(user_id)
    return ctx.client if ctx else None


def _get_trade_db(context: ContextTypes.DEFAULT_TYPE, user_id: str):
    orchestrator = _get_orchestrator(context)
    if not orchestrator:
        return None
    ctx = orchestrator.pipelines.get(user_id)
    return ctx.db if ctx else None


def _is_pipeline_active(context: ContextTypes.DEFAULT_TYPE, user_id: str) -> bool:
    orchestrator = _get_orchestrator(context)
    if not orchestrator:
        return False
    paused = orchestrator.is_user_paused(user_id)
    if paused is None:
        return False
    return not paused


def _resolve_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str | None:
    """Resolve user_id from chat_id."""
    user_db = _get_user_db(context)
    return user_db.get_user_by_telegram_chat_id(chat_id)


# ------------------------------------------------------------------
# Calls-view tracking
# ------------------------------------------------------------------

def _enter_calls_view(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Mark *chat_id* as currently viewing the Calls screen."""
    context.bot_data.setdefault("calls_view_users", set()).add(chat_id)


def _leave_calls_view(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Remove *chat_id* from the calls-view set (if present)."""
    context.bot_data.setdefault("calls_view_users", set()).discard(chat_id)
    # Clean up tracked message id
    context.bot_data.setdefault("calls_view_msg", {}).pop(chat_id, None)


def set_calls_view_msg(bot_data: dict, chat_id: int, message_id: int) -> None:
    """Store the message_id of the current Calls View message for *chat_id*."""
    bot_data.setdefault("calls_view_msg", {})[chat_id] = message_id


def get_calls_view_msg(bot_data: dict, chat_id: int) -> int | None:
    """Return the stored Calls View message_id, or None."""
    return bot_data.get("calls_view_msg", {}).get(chat_id)


def is_in_calls_view(bot_data: dict, chat_id: int) -> bool:
    """Return True if *chat_id* is currently in Calls View mode."""
    return chat_id in bot_data.get("calls_view_users", set())


# ------------------------------------------------------------------
# Main menu text
# ------------------------------------------------------------------

def _build_main_menu_text(user_db: UserDatabase, user_id: str) -> str:
    """Build the main menu text with user info."""
    user = user_db.get_user(user_id)
    display_name = user.display_name if user else "User"

    # Get wallet address
    try:
        creds = user_db.get_user_credentials_decrypted(user_id)
        if creds:
            wallet = mask_address(creds.get("account_address", "N/A"))
            network = creds.get("network", "testnet").capitalize()
        else:
            wallet = "N/A"
            network = "Testnet"
    except Exception:
        wallet = "N/A"
        network = "Testnet"

    return (
        f"{'━' * 30}\n"
        "🧪 *Welcome to Potion Perps!*\n"
        f"{'━' * 30}\n\n"
        f"Hey {display_name}! What would you like to do?\n\n"
        f"💼 Wallet:    `{wallet}`\n"
        f"🌐 Network:  {network}\n\n"
        f"{'─' * 30}\n"
        "📖 Guide  ·  💬 Support  ·  🌐 Socials"
    )


# ------------------------------------------------------------------
# Screen builders for each submenu
# ------------------------------------------------------------------

def _build_account_text(context: ContextTypes.DEFAULT_TYPE, user_id: str) -> str:
    user_db = _get_user_db(context)
    user_config = user_db.get_user_config(user_id)
    expires_at = user_db.get_access_expiry(user_id)
    try:
        creds = user_db.get_user_credentials_decrypted(user_id)
        if not creds:
            creds = {}
    except Exception:
        creds = {}
    return format_account_info(user_config, creds, expires_at)


def _build_calls_text(context: ContextTypes.DEFAULT_TYPE, user_id: str) -> tuple[str, InlineKeyboardMarkup]:
    """Build calls view text and keyboard with approve/reject for pending signals."""
    trade_db = _get_trade_db(context, user_id)
    if not trade_db:
        return format_calls_view([]), calls_view_keyboard()

    # Get all recent trades (last N) — combine open + completed
    all_trades = trade_db.get_open_trades() + trade_db.get_completed_trades()
    # Sort by created_at descending to pick the N most recent…
    all_trades.sort(key=lambda t: t.created_at, reverse=True)
    recent = all_trades[:CALLS_LIMIT]
    # …then reverse so oldest is on top, newest at bottom (near buttons)
    recent.reverse()

    pending_ids = [t.trade_id for t in recent if t.status == TradeStatus.PENDING]

    text = format_calls_view(recent)

    # Build keyboard with approve/reject for pending signals
    keyboard_rows = []
    for tid in pending_ids:
        keyboard_rows.append([
            InlineKeyboardButton(f"✅ Approve #{tid}", callback_data=f"signal:approve:{tid}"),
            InlineKeyboardButton(f"❌ Reject #{tid}", callback_data=f"signal:reject:{tid}"),
        ])

    # Standard nav
    keyboard_rows.extend([
        [
            InlineKeyboardButton("⬅️ Menu", callback_data="menu:main"),
            InlineKeyboardButton("🔄 Refresh", callback_data="menu:refresh"),
        ],
        [InlineKeyboardButton("✖ Close", callback_data="menu:close")],
    ])

    return text, InlineKeyboardMarkup(keyboard_rows)


def _build_trading_hub_text(context: ContextTypes.DEFAULT_TYPE, user_id: str) -> str:
    client = _get_client(context, user_id)
    trade_db = _get_trade_db(context, user_id)

    balance = None
    positions = None
    open_trades = 0

    if client:
        try:
            balance = client.get_balance()
            positions = client.get_open_positions()
        except Exception as e:
            logger.error("Failed to fetch trading data for user %s: %s", user_id, e)

    if trade_db:
        try:
            open_trades = len(trade_db.get_open_trades())
        except Exception:
            pass

    return format_trading_hub(balance, positions, open_trades)


def _build_stats_text(context: ContextTypes.DEFAULT_TYPE, user_id: str) -> str:
    trade_db = _get_trade_db(context, user_id)
    if not trade_db:
        return "📈 *Trading Statistics*\n\nPipeline not active."

    closed = trade_db.get_trades_by_status(TradeStatus.CLOSED)
    open_trades = trade_db.get_open_trades()
    return format_stats(closed, len(open_trades))


def _build_dashboard_text(context: ContextTypes.DEFAULT_TYPE, user_id: str) -> str:
    user_db = _get_user_db(context)
    user_config = user_db.get_user_config(user_id)
    expires_at = user_db.get_access_expiry(user_id)
    is_active = _is_pipeline_active(context, user_id)
    return format_dashboard(user_config, is_active, expires_at)


def _build_config_text(context: ContextTypes.DEFAULT_TYPE, user_id: str) -> str:
    from src.config.settings import BUILTIN_PRESETS

    user_db = _get_user_db(context)
    cfg = user_db.get_user_config(user_id)

    preset = cfg.get("active_preset", "runner")
    auto = "✅ ON" if cfg.get("auto_execute") else "❌ OFF"
    lev = cfg.get("max_leverage", 20)

    p = BUILTIN_PRESETS.get(preset)
    tp_desc = ""
    if p:
        tp_pcts = [int(x * 100) for x in p.tp_split]
        tp_desc = f" ({tp_pcts[0]}/{tp_pcts[1]}/{tp_pcts[2]})"

    return (
        "⚙️ *Configuration*\n\n"
        f"🎯 Strategy: {preset}{tp_desc}\n"
        f"⚡ Auto-execute: {auto}\n"
        f"📊 Max Leverage: {lev}x\n\n"
        "🔒 *Risk Limits*\n"
        f"Max Positions: {cfg.get('max_open_positions', 10)}\n"
        f"Max Position Size: ${cfg.get('max_position_size_usd', 500):,.0f}\n"
        f"Max Exposure: ${cfg.get('max_total_exposure_usd', 2000):,.0f}\n"
        f"Daily Loss Limit: {cfg.get('max_daily_loss_pct', 10)}%"
    )


# ------------------------------------------------------------------
# Screen prefix → rebuild mapping (for refresh)
# ------------------------------------------------------------------

# Screen detection: Telegram returns plain text (markdown stripped).
# Use plain-text prefixes and check longer prefixes first.
_SCREEN_MAP = [
    ("📊 Trading\n", "trading"),       # Must be before stats (also 📊)
    ("📈 Trading Statistics", "stats"),
    ("🧪 Welcome", "main"),
    ("👤 Account", "account"),
    ("📡 Calls View", "calls"),
    ("🛡 Risk Dashboard", "dashboard"),
    ("⚙️ Configuration", "config"),
]


def _detect_current_screen(message_text: str | None) -> str:
    """Detect which screen is currently displayed from the message text prefix."""
    if not message_text:
        return "main"
    for prefix, screen in _SCREEN_MAP:
        if message_text.startswith(prefix):
            return screen
    return "main"


# ------------------------------------------------------------------
# Command handler
# ------------------------------------------------------------------

@registered_only
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /menu — send main menu as a new message."""
    user_id = context.user_data["user_id"]
    user_db = _get_user_db(context)
    text = _build_main_menu_text(user_db, user_id)
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


# ------------------------------------------------------------------
# Callback handler — routes all menu:* patterns
# ------------------------------------------------------------------

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all menu:* callbacks."""
    query = update.callback_query
    await query.answer()

    data = query.data
    chat_id = update.effective_chat.id

    # Close — delete the message
    if data == "menu:close":
        _leave_calls_view(context, chat_id)
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    # Resolve user
    user_id = _resolve_user(context, chat_id)
    if not user_id:
        await query.edit_message_text("You're not registered. Use /register to get started.")
        return

    user_db = _get_user_db(context)

    try:
        if data == "menu:main":
            _leave_calls_view(context, chat_id)
            text = _build_main_menu_text(user_db, user_id)
            await query.edit_message_text(
                text, parse_mode="Markdown", reply_markup=main_menu_keyboard(),
            )

        elif data == "menu:account":
            _leave_calls_view(context, chat_id)
            text = _build_account_text(context, user_id)
            await query.edit_message_text(
                text, parse_mode="Markdown", reply_markup=account_keyboard(),
            )

        elif data == "menu:calls":
            _enter_calls_view(context, chat_id)
            text, keyboard = _build_calls_text(context, user_id)
            await query.edit_message_text(
                text, parse_mode="Markdown", reply_markup=keyboard,
            )
            set_calls_view_msg(context.bot_data, chat_id, query.message.message_id)

        elif data == "menu:trading":
            _leave_calls_view(context, chat_id)
            text = _build_trading_hub_text(context, user_id)
            await query.edit_message_text(
                text, parse_mode="Markdown", reply_markup=trading_hub_keyboard(),
            )

        elif data == "menu:stats":
            _leave_calls_view(context, chat_id)
            text = _build_stats_text(context, user_id)
            await query.edit_message_text(
                text, parse_mode="Markdown", reply_markup=stats_keyboard(),
            )

        elif data == "menu:dashboard":
            _leave_calls_view(context, chat_id)
            text = _build_dashboard_text(context, user_id)
            await query.edit_message_text(
                text, parse_mode="Markdown", reply_markup=dashboard_keyboard(),
            )

        elif data == "menu:config":
            _leave_calls_view(context, chat_id)
            text = _build_config_text(context, user_id)
            is_active = _is_pipeline_active(context, user_id)
            await query.edit_message_text(
                text, parse_mode="Markdown", reply_markup=config_menu_keyboard(is_active),
            )

        elif data == "menu:refresh":
            # Detect current screen and re-render
            current_text = query.message.text if query.message else None
            screen = _detect_current_screen(current_text)
            await _refresh_screen(query, context, user_id, user_db, screen)

    except Exception as e:
        err_msg = str(e)
        # Telegram raises this when edit content is identical — not a real error
        if "Message is not modified" in err_msg:
            logger.debug("Refresh: content unchanged for user %s", user_id)
            return
        logger.exception("Error handling menu callback %s for user %s", data, user_id)
        try:
            await query.edit_message_text("⚠️ Something went wrong. Try again.")
        except Exception:
            pass


async def _refresh_screen(query, context, user_id, user_db, screen):
    """Re-render the detected screen."""
    if screen == "main":
        text = _build_main_menu_text(user_db, user_id)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())

    elif screen == "account":
        text = _build_account_text(context, user_id)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=account_keyboard())

    elif screen == "calls":
        text, keyboard = _build_calls_text(context, user_id)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

    elif screen == "trading":
        text = _build_trading_hub_text(context, user_id)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=trading_hub_keyboard())

    elif screen == "stats":
        text = _build_stats_text(context, user_id)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=stats_keyboard())

    elif screen == "dashboard":
        text = _build_dashboard_text(context, user_id)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=dashboard_keyboard())

    elif screen == "config":
        text = _build_config_text(context, user_id)
        is_active = _is_pipeline_active(context, user_id)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=config_menu_keyboard(is_active))

    else:
        text = _build_main_menu_text(user_db, user_id)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())
