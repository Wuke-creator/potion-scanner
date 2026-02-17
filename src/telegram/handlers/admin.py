"""Admin command handlers — invite code & user management."""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.orchestrator import Orchestrator
from src.state.user_db import UserDatabase
from src.telegram.invite_codes import generate_invite_code
from src.telegram.middleware import admin_only

logger = logging.getLogger(__name__)


def _get_user_db(context: ContextTypes.DEFAULT_TYPE) -> UserDatabase:
    return context.bot_data["user_db"]


def _get_orchestrator(context: ContextTypes.DEFAULT_TYPE) -> Orchestrator:
    return context.bot_data["orchestrator"]


ADMIN_HELP_MESSAGE = (
    "*Admin Commands*\n\n"
    "*Invite Codes*\n"
    "/generate\\_code \\[days\\] — Generate invite code\n"
    "/generate\\_codes <count> \\[days\\] — Batch generate codes\n"
    "/list\\_codes — List all invite codes\n"
    "/revoke\\_code <code> — Revoke an invite code\n\n"
    "*User Management*\n"
    "/users — List all registered users\n"
    "/extend <user\\_id> <days> — Extend user access\n"
    "/revoke <user\\_id> — Revoke user access\n\n"
    "*Emergency*\n"
    "/kill — Kill switch (close all positions)\n"
    "/resume — Resume after kill switch\n\n"
    "*Communication*\n"
    "/broadcast <message> — Message all active users\n\n"
    "*Testing*\n"
    "/inject — ⚠️ TESTING ONLY — Inject fake signal\n\n"
    "*Admin Access*\n"
    "/add\\_admin <telegram\\_id> — Grant admin access\n"
    "/remove\\_admin <telegram\\_id> — Revoke admin access\n"
    "/list\\_admins — List all admins"
)


@admin_only
async def admin_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /admin — show all admin commands."""
    await update.message.reply_text(ADMIN_HELP_MESSAGE, parse_mode="Markdown")


@admin_only
async def generate_code_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /generate_code [days] — generate a single invite code."""
    user_db = _get_user_db(context)
    admin_name = str(update.effective_user.id)

    # Parse optional duration
    duration_days = None
    if context.args:
        try:
            duration_days = int(context.args[0])
            if duration_days < 1:
                await update.message.reply_text("Duration must be at least 1 day.")
                return
        except ValueError:
            await update.message.reply_text("Usage: /generate\\_code \\[days\\]\nExample: `/generate_code 30`", parse_mode="Markdown")
            return

    code = generate_invite_code()
    user_db.create_invite_code(code, created_by=admin_name, duration_days=duration_days)

    duration_text = f"{duration_days} days" if duration_days else "unlimited"
    await update.message.reply_text(
        f"*Invite Code Generated*\n\n"
        f"`{code}`\n\n"
        f"Duration: {duration_text}",
        parse_mode="Markdown",
    )


@admin_only
async def generate_codes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /generate_codes <count> [days] — batch generate invite codes."""
    user_db = _get_user_db(context)
    admin_name = str(update.effective_user.id)

    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /generate\\_codes \\<count\\> \\[days\\]\n"
            "Example: `/generate_codes 5 30`",
            parse_mode="Markdown",
        )
        return

    try:
        count = int(context.args[0])
        if count < 1 or count > 50:
            await update.message.reply_text("Count must be between 1 and 50.")
            return
    except ValueError:
        await update.message.reply_text("Count must be a number.")
        return

    duration_days = None
    if len(context.args) >= 2:
        try:
            duration_days = int(context.args[1])
            if duration_days < 1:
                await update.message.reply_text("Duration must be at least 1 day.")
                return
        except ValueError:
            await update.message.reply_text("Duration must be a number.")
            return

    codes = []
    for _ in range(count):
        code = generate_invite_code()
        user_db.create_invite_code(code, created_by=admin_name, duration_days=duration_days)
        codes.append(code)

    duration_text = f"{duration_days} days" if duration_days else "unlimited"
    code_list = "\n".join(f"`{c}`" for c in codes)
    await update.message.reply_text(
        f"*{count} Invite Codes Generated*\n"
        f"Duration: {duration_text}\n\n{code_list}",
        parse_mode="Markdown",
    )


@admin_only
async def list_codes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /list_codes — show all invite codes with status."""
    user_db = _get_user_db(context)
    codes = user_db.list_invite_codes()

    if not codes:
        await update.message.reply_text("No invite codes found.")
        return

    lines = ["*Invite Codes*\n"]
    for c in codes:
        status_icon = {
            "active": "🟢",
            "redeemed": "🔵",
            "revoked": "🔴",
            "expired": "⚪",
        }.get(c["status"], "❓")

        duration = f"{c['duration_days']}d" if c["duration_days"] else "∞"
        line = f"{status_icon} `{c['code']}` — {c['status']} ({duration})"

        if c["redeemed_by"]:
            line += f" → {c['redeemed_by']}"

        lines.append(line)

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@admin_only
async def revoke_code_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /revoke_code <code> — revoke an unused invite code."""
    user_db = _get_user_db(context)

    if not context.args:
        await update.message.reply_text(
            "Usage: /revoke\\_code \\<code\\>\nExample: `/revoke_code PPB-A3F8-K9X2`",
            parse_mode="Markdown",
        )
        return

    code = context.args[0].upper()
    if user_db.revoke_invite_code(code):
        await update.message.reply_text(f"Code `{code}` has been revoked.", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            f"Could not revoke `{code}`. It may not exist or is already redeemed/revoked.",
            parse_mode="Markdown",
        )


# ------------------------------------------------------------------
# User management commands
# ------------------------------------------------------------------


@admin_only
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /users — list all registered users with status."""
    user_db = _get_user_db(context)
    users = user_db.list_users()

    if not users:
        await update.message.reply_text("No users registered.")
        return

    lines = ["*Registered Users*\n"]
    for u in users:
        icon = "\U0001f7e2" if u.status == "active" else "\U0001f534"
        config = user_db.get_user_config(u.user_id)
        preset = config.get("active_preset", "—") if config else "—"
        expiry = user_db.get_access_expiry(u.user_id)
        expiry_text = expiry[:10] if expiry else "unlimited"
        name = u.display_name[:20] if u.display_name else u.user_id
        lines.append(f"{icon} `{u.user_id}` — {name} | {preset} | exp: {expiry_text}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@admin_only
async def extend_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /extend <user_id> <days> — extend user access."""
    user_db = _get_user_db(context)

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /extend <user\\_id> <days>\nExample: `/extend 7441245554 30`",
            parse_mode="Markdown",
        )
        return

    target_user_id = context.args[0]
    try:
        days = int(context.args[1])
        if days < 1:
            await update.message.reply_text("Days must be at least 1.")
            return
    except ValueError:
        await update.message.reply_text("Days must be a number.")
        return

    user = user_db.get_user(target_user_id)
    if not user:
        await update.message.reply_text(f"User `{target_user_id}` not found.", parse_mode="Markdown")
        return

    new_expiry = user_db.extend_user_access(target_user_id, days)

    # Re-activate if user was inactive (expired)
    reactivated = ""
    if user.status == "inactive":
        user_db.set_user_status(target_user_id, "active")
        orchestrator = _get_orchestrator(context)
        try:
            orchestrator.activate_user(target_user_id)
            reactivated = "\nUser re-activated and pipeline started."
        except Exception as e:
            reactivated = f"\nUser status set to active. Pipeline activation failed: {e}"

    await update.message.reply_text(
        f"Access extended for `{target_user_id}` by {days} days.\n"
        f"New expiry: `{new_expiry[:19]}`{reactivated}",
        parse_mode="Markdown",
    )


@admin_only
async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /revoke <user_id> — revoke user access and deactivate pipeline."""
    user_db = _get_user_db(context)
    orchestrator = _get_orchestrator(context)

    if not context.args:
        await update.message.reply_text(
            "Usage: /revoke <user\\_id>\nExample: `/revoke 7441245554`",
            parse_mode="Markdown",
        )
        return

    target_user_id = context.args[0]
    user = user_db.get_user(target_user_id)
    if not user:
        await update.message.reply_text(f"User `{target_user_id}` not found.", parse_mode="Markdown")
        return

    user_db.revoke_user_access(target_user_id)
    orchestrator.deactivate_user(target_user_id)
    await update.message.reply_text(
        f"Access revoked and pipeline deactivated for `{target_user_id}`.",
        parse_mode="Markdown",
    )


@admin_only
async def kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /kill — show confirmation before activating kill switch."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Confirm Kill", callback_data="admin:kill_confirm"),
            InlineKeyboardButton("Cancel", callback_data="admin:kill_cancel"),
        ]
    ])
    await update.message.reply_text(
        "Are you sure you want to activate the kill switch?\n"
        "This will cancel all orders and close all positions for ALL users.",
        reply_markup=keyboard,
    )


@admin_only
async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /resume — resume signal processing after kill switch."""
    orchestrator = _get_orchestrator(context)
    orchestrator.resume()
    await update.message.reply_text("Kill switch deactivated. Signal processing resumed.")


@admin_only
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /broadcast <message> — send message to all active users."""
    user_db = _get_user_db(context)

    if not context.args:
        await update.message.reply_text(
            "Usage: /broadcast <message>\nExample: `/broadcast Maintenance in 1 hour`",
            parse_mode="Markdown",
        )
        return

    message = " ".join(context.args)
    chat_ids = user_db.get_all_telegram_chat_ids()

    if not chat_ids:
        await update.message.reply_text("No active users to broadcast to.")
        return

    success = 0
    failed = 0
    for chat_id in chat_ids:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"*Admin Broadcast*\n\n{message}",
                parse_mode="Markdown",
            )
            success += 1
        except Exception:
            logger.exception("Failed to send broadcast to chat %s", chat_id)
            failed += 1

    await update.message.reply_text(
        f"Broadcast sent. Delivered: {success}, Failed: {failed}"
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin inline button callbacks (kill confirm/cancel)."""
    query = update.callback_query
    await query.answer()

    # Check admin
    admin_ids: list[int] = context.bot_data.get("admin_ids", [])
    if update.effective_user.id not in admin_ids:
        await query.edit_message_text("This action is only available to administrators.")
        return

    action = query.data

    if action == "admin:kill_confirm":
        orchestrator = _get_orchestrator(context)
        results = orchestrator.kill_all()

        lines = ["*Kill Switch Activated*\n"]
        for user_id, result in results.items():
            lines.append(
                f"User `{user_id}`: {result['closed']} closed, "
                f"{len(result.get('errors', []))} errors"
            )
        if not results:
            lines.append("No active pipelines.")

        await query.edit_message_text("\n".join(lines), parse_mode="Markdown")

    elif action == "admin:kill_cancel":
        await query.edit_message_text("Kill switch canceled.")


# ------------------------------------------------------------------
# Admin access management
# ------------------------------------------------------------------


@admin_only
async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /add_admin <telegram_id> — grant admin access."""
    user_db = _get_user_db(context)

    if not context.args:
        await update.message.reply_text(
            "Usage: /add\\_admin <telegram\\_id>\nExample: `/add_admin 123456789`",
            parse_mode="Markdown",
        )
        return

    try:
        new_admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Telegram ID must be a number.")
        return

    added_by = update.effective_user.id
    if user_db.add_telegram_admin(new_admin_id, added_by):
        # Add to runtime list so it takes effect immediately
        admin_ids: list[int] = context.bot_data.get("admin_ids", [])
        if new_admin_id not in admin_ids:
            admin_ids.append(new_admin_id)
        await update.message.reply_text(
            f"Admin access granted to `{new_admin_id}`.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"`{new_admin_id}` is already an admin.",
            parse_mode="Markdown",
        )


@admin_only
async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /remove_admin <telegram_id> — revoke admin access."""
    user_db = _get_user_db(context)

    if not context.args:
        await update.message.reply_text(
            "Usage: /remove\\_admin <telegram\\_id>\nExample: `/remove_admin 123456789`",
            parse_mode="Markdown",
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Telegram ID must be a number.")
        return

    # Prevent removing yourself
    if target_id == update.effective_user.id:
        await update.message.reply_text("You cannot remove yourself as admin.")
        return

    if user_db.remove_telegram_admin(target_id):
        # Remove from runtime list
        admin_ids: list[int] = context.bot_data.get("admin_ids", [])
        if target_id in admin_ids:
            admin_ids.remove(target_id)
        await update.message.reply_text(
            f"Admin access revoked for `{target_id}`.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"`{target_id}` is not a dynamically added admin.\n"
            "Admins set via TELEGRAM\\_ADMIN\\_IDS env var cannot be removed here.",
            parse_mode="Markdown",
        )


@admin_only
async def list_admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /list_admins — show all admin IDs."""
    admin_ids: list[int] = context.bot_data.get("admin_ids", [])

    if not admin_ids:
        await update.message.reply_text("No admins configured.")
        return

    lines = ["*Admin Users*\n"]
    for aid in admin_ids:
        lines.append(f"`{aid}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ------------------------------------------------------------------
# ⚠️  TESTING ONLY — Signal injection for development/QA
# ------------------------------------------------------------------

# Track the last injected trade ID so TP/close signals target the right trade
_last_inject_trade_id: int | None = None


def _get_any_client(context: ContextTypes.DEFAULT_TYPE):
    """Get a HyperliquidClient from any active pipeline (for price fetching)."""
    orchestrator = _get_orchestrator(context)
    for ctx in orchestrator.pipelines.values():
        return ctx.client
    return None


_INJECT_COINS = ["ETH", "BTC", "SOL", "XRP"]
_INJECT_SIDES = ["LONG", "SHORT"]
_INJECT_RISKS = ["LOW", "MEDIUM", "HIGH"]
_INJECT_TYPES = ["SWING", "SCALP"]

# Track last injected coin so TP signals reference the correct pair
_last_inject_coin: str | None = None


def _build_signal(price: float, trade_id: int, coin: str, side: str, leverage: int, risk: str, trade_type: str) -> str:
    """Build a signal around the current price with randomized parameters."""
    if side == "LONG":
        entry = round(price * 1.005, 2)
        sl = round(price * 0.97, 2)
        tp1 = round(price * 1.01, 2)
        tp2 = round(price * 1.02, 2)
        tp3 = round(price * 1.03, 2)
    else:
        entry = round(price * 0.995, 2)
        sl = round(price * 1.03, 2)
        tp1 = round(price * 0.99, 2)
        tp2 = round(price * 0.98, 2)
        tp3 = round(price * 0.97, 2)

    return (
        f"TRADING SIGNAL ALERT\n\n"
        f"PAIR: {coin}/USDT #{trade_id}\n"
        f"({risk} RISK)\n\n"
        f"TYPE: {trade_type}\n"
        f"SIZE: 1-4%\n"
        f"SIDE: {side}\n\n"
        f"ENTRY: {entry}\n"
        f"SL: {sl}          (-3.00%)\n\n"
        f"TAKE PROFIT TARGETS:\n\n"
        f"TP1: {tp1}      (1.00%)\n"
        f"TP2: {tp2}      (2.00%)\n"
        f"TP3: {tp3}      (3.00%)\n\n"
        f"LEVERAGE: {leverage}x\n\n"
        f"PROTECT YOUR CAPITAL, MANAGE RISK, LETS PRINT!"
    )


def _build_tp_signal(tp_num: str, profit: str, trade_id: int, coin: str) -> str:
    """Build a TP hit signal for the given trade."""
    if tp_num == "all":
        return (
            f"**\U0001f525ALL TAKE-PROFIT TARGETS HIT**\n\n"
            f"**\U0001f4ddPAIR:** {coin}/USDT #{trade_id}\n\n"
            f"**\U0001f4b0PROFIT:** {profit} \U0001f4c8"
        )
    return (
        f"**\u2705 TP TARGET {tp_num} HIT**\n\n"
        f"**\U0001f4ddPAIR:** {coin}/USDT #{trade_id}\n\n"
        f"**\U0001f4b0PROFIT:** {profit} \U0001f4c8"
    )


@admin_only
async def inject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /inject — show test signal buttons for quick injection."""
    global _last_inject_trade_id

    # Show current prices for context
    client = _get_any_client(context)
    price_lines = []
    if client:
        try:
            mids = client.get_all_mids()
            for coin in _INJECT_COINS:
                p = float(mids.get(coin, "0"))
                if p:
                    price_lines.append(f"{coin}: ${p:,.2f}")
        except Exception:
            pass

    price_info = "\n" + " | ".join(price_lines) + "\n" if price_lines else ""

    trade_id_info = ""
    if _last_inject_trade_id:
        trade_id_info = f"\nLast injected trade: #{_last_inject_trade_id}\n"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("New Random Signal", callback_data="inject:signal")],
        [
            InlineKeyboardButton("TP1 Hit", callback_data="inject:tp1"),
            InlineKeyboardButton("TP2 Hit", callback_data="inject:tp2"),
        ],
        [InlineKeyboardButton("All TP Hit", callback_data="inject:all_tp")],
    ])
    await update.message.reply_text(
        f"*Test Signal Injection*\n{price_info}{trade_id_info}\n"
        f"Coins: {', '.join(_INJECT_COINS)} | Leverage: 3-15x | LONG/SHORT\n"
        f"Select a signal to inject into the pipeline for all active users:",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def inject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inject:{signal_type} callbacks — dispatch test signal to all users."""
    global _last_inject_trade_id

    query = update.callback_query
    await query.answer()

    # Admin check
    admin_ids: list[int] = context.bot_data.get("admin_ids", [])
    if update.effective_user.id not in admin_ids:
        await query.edit_message_text("This action is only available to administrators.")
        return

    signal_type = query.data.replace("inject:", "")
    orchestrator = _get_orchestrator(context)

    if signal_type == "signal":
        global _last_inject_coin

        client = _get_any_client(context)
        if not client:
            await query.edit_message_text("No active pipelines — cannot fetch price.")
            return

        import random
        coin = random.choice(_INJECT_COINS)
        side = random.choice(_INJECT_SIDES)
        leverage = random.randint(3, 15)
        risk = random.choice(_INJECT_RISKS)
        trade_type = random.choice(_INJECT_TYPES)

        try:
            mids = client.get_all_mids()
            price = float(mids.get(coin, "0"))
        except Exception as e:
            await query.edit_message_text(f"Failed to fetch {coin} price: {e}")
            return

        if not price:
            await query.edit_message_text(f"Could not get {coin} price.")
            return

        trade_id = random.randint(80000, 89999)
        _last_inject_trade_id = trade_id
        _last_inject_coin = coin

        raw_signal = _build_signal(price, trade_id, coin, side, leverage, risk, trade_type)
        orchestrator.dispatch(raw_signal)

        pipelines = len(orchestrator.pipelines)
        paused = sum(1 for ctx in orchestrator.pipelines.values() if ctx.paused)
        await query.edit_message_text(
            f"*New Signal #{trade_id}* injected ({coin} {side} {leverage}x @ ${price:,.2f}).\n"
            f"Dispatched to {pipelines - paused} active pipeline(s).",
            parse_mode="Markdown",
        )

    elif signal_type in ("tp1", "tp2", "all_tp"):
        if not _last_inject_trade_id:
            await query.edit_message_text("No test trade active. Inject a New Signal first.")
            return

        tp_map = {
            "tp1": ("1", "1.00%"),
            "tp2": ("2", "2.00%"),
            "all_tp": ("all", "3.00%"),
        }
        tp_num, profit = tp_map[signal_type]
        coin = _last_inject_coin or "ETH"
        raw_signal = _build_tp_signal(tp_num, profit, _last_inject_trade_id, coin)
        orchestrator.dispatch(raw_signal)

        label = {"tp1": "TP1 Hit", "tp2": "TP2 Hit", "all_tp": "All TP Hit"}
        pipelines = len(orchestrator.pipelines)
        paused = sum(1 for ctx in orchestrator.pipelines.values() if ctx.paused)
        await query.edit_message_text(
            f"*{label[signal_type]}* for trade #{_last_inject_trade_id} injected.\n"
            f"Dispatched to {pipelines - paused} active pipeline(s).",
            parse_mode="Markdown",
        )

    else:
        await query.edit_message_text(f"Unknown signal type: {signal_type}")
