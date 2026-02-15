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
    await update.message.reply_text(
        f"Access extended for `{target_user_id}` by {days} days.\n"
        f"New expiry: `{new_expiry[:19]}`",
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
