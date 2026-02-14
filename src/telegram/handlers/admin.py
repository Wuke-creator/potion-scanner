"""Admin command handlers — invite code management."""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from src.state.user_db import UserDatabase
from src.telegram.invite_codes import generate_invite_code
from src.telegram.middleware import admin_only

logger = logging.getLogger(__name__)


def _get_user_db(context: ContextTypes.DEFAULT_TYPE) -> UserDatabase:
    return context.bot_data["user_db"]


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
