"""Configuration menu handlers — /config, /preset, /auto, inline callbacks."""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from src.config.settings import BUILTIN_PRESETS
from src.state.user_db import UserDatabase
from src.telegram.keyboards import config_main_keyboard, preset_keyboard, risk_keyboard
from src.telegram.middleware import registered_only

logger = logging.getLogger(__name__)


def _get_user_db(context: ContextTypes.DEFAULT_TYPE) -> UserDatabase:
    return context.bot_data["user_db"]


def _format_config(cfg: dict) -> str:
    """Format current config for display."""
    preset = cfg.get("active_preset", "runner")
    auto = "ON" if cfg.get("auto_execute") else "OFF"
    lev = cfg.get("max_leverage", 20)

    # Get preset details
    p = BUILTIN_PRESETS.get(preset)
    tp_desc = ""
    if p:
        tp_pcts = [int(x * 100) for x in p.tp_split]
        tp_desc = f" ({tp_pcts[0]}/{tp_pcts[1]}/{tp_pcts[2]})"

    return (
        f"*Current Configuration*\n\n"
        f"Strategy: {preset}{tp_desc}\n"
        f"Auto-execute: {auto}\n"
        f"Max Leverage: {lev}x\n\n"
        f"*Risk Limits*\n"
        f"Max Positions: {cfg.get('max_open_positions', 10)}\n"
        f"Max Position Size: ${cfg.get('max_position_size_usd', 500):,.0f}\n"
        f"Max Exposure: ${cfg.get('max_total_exposure_usd', 2000):,.0f}\n"
        f"Daily Loss Limit: {cfg.get('max_daily_loss_pct', 10)}%"
    )


@registered_only
async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /config — show current settings with inline menu."""
    user_id = context.user_data["user_id"]
    user_db = _get_user_db(context)
    cfg = user_db.get_user_config(user_id)

    text = _format_config(cfg)
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=config_main_keyboard(),
    )


@registered_only
async def preset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /preset <name> — quick preset change via command."""
    user_id = context.user_data["user_id"]
    user_db = _get_user_db(context)

    if not context.args:
        names = ", ".join(sorted(BUILTIN_PRESETS.keys()))
        await update.message.reply_text(
            f"Usage: /preset <name>\nAvailable: {names}"
        )
        return

    name = context.args[0].lower()
    if name not in BUILTIN_PRESETS:
        names = ", ".join(sorted(BUILTIN_PRESETS.keys()))
        await update.message.reply_text(f"Unknown preset. Available: {names}")
        return

    user_db.update_user_config(user_id, active_preset=name)
    p = BUILTIN_PRESETS[name]
    tp_pcts = [int(x * 100) for x in p.tp_split]
    await update.message.reply_text(
        f"Preset changed to *{name}* ({tp_pcts[0]}/{tp_pcts[1]}/{tp_pcts[2]})",
        parse_mode="Markdown",
    )


@registered_only
async def auto_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /auto — toggle auto-execute."""
    user_id = context.user_data["user_id"]
    user_db = _get_user_db(context)
    cfg = user_db.get_user_config(user_id)

    new_value = not cfg.get("auto_execute", False)
    user_db.update_user_config(user_id, auto_execute=new_value)
    state = "ON" if new_value else "OFF"
    await update.message.reply_text(f"Auto-execute: *{state}*", parse_mode="Markdown")


async def config_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all config-related inline keyboard callbacks."""
    query = update.callback_query
    await query.answer()

    user_db = _get_user_db(context)
    chat_id = update.effective_chat.id
    user_id = user_db.get_user_by_telegram_chat_id(chat_id)
    if not user_id:
        await query.edit_message_text("You're not registered. Use /register.")
        return

    data = query.data  # e.g. "cfg:strategy", "cfg:preset:runner", "cfg:auto", "cfg:risk", etc.

    if data == "cfg:strategy":
        await query.edit_message_text(
            "Select a strategy preset:",
            reply_markup=preset_keyboard(),
        )

    elif data.startswith("cfg:preset:"):
        name = data.split(":", 2)[2]
        if name not in BUILTIN_PRESETS:
            await query.edit_message_text("Unknown preset.")
            return

        user_db.update_user_config(user_id, active_preset=name)
        p = BUILTIN_PRESETS[name]
        tp_pcts = [int(x * 100) for x in p.tp_split]

        cfg = user_db.get_user_config(user_id)
        text = _format_config(cfg)
        text += f"\n\n_Changed to {name} ({tp_pcts[0]}/{tp_pcts[1]}/{tp_pcts[2]})_"
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=config_main_keyboard(),
        )

    elif data == "cfg:auto":
        cfg = user_db.get_user_config(user_id)
        new_value = not cfg.get("auto_execute", False)
        user_db.update_user_config(user_id, auto_execute=new_value)

        cfg = user_db.get_user_config(user_id)
        text = _format_config(cfg)
        state = "ON" if new_value else "OFF"
        text += f"\n\n_Auto-execute toggled {state}_"
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=config_main_keyboard(),
        )

    elif data == "cfg:risk":
        cfg = user_db.get_user_config(user_id)
        await query.edit_message_text(
            "*Risk Limits*\n\n"
            f"Max Positions: {cfg.get('max_open_positions', 10)}\n"
            f"Max Position Size: ${cfg.get('max_position_size_usd', 500):,.0f}\n"
            f"Max Exposure: ${cfg.get('max_total_exposure_usd', 2000):,.0f}\n"
            f"Daily Loss Limit: {cfg.get('max_daily_loss_pct', 10)}%\n\n"
            "Select a limit to change:",
            parse_mode="Markdown",
            reply_markup=risk_keyboard(),
        )

    elif data == "cfg:leverage":
        # Store that we're waiting for leverage input
        context.user_data["awaiting_config"] = "max_leverage"
        cfg = user_db.get_user_config(user_id)
        await query.edit_message_text(
            f"Current max leverage: {cfg.get('max_leverage', 20)}x\n\n"
            "Send the new max leverage value (1-50):"
        )

    elif data.startswith("cfg:risk:"):
        field = data.split(":", 2)[2]
        field_labels = {
            "max_open_positions": "Max Open Positions",
            "max_position_size_usd": "Max Position Size (USD)",
            "max_total_exposure_usd": "Max Total Exposure (USD)",
            "max_daily_loss_pct": "Max Daily Loss (%)",
        }
        label = field_labels.get(field, field)
        context.user_data["awaiting_config"] = field
        cfg = user_db.get_user_config(user_id)
        current = cfg.get(field, "?")
        await query.edit_message_text(
            f"Current {label}: {current}\n\nSend the new value:"
        )

    elif data == "cfg:back":
        cfg = user_db.get_user_config(user_id)
        text = _format_config(cfg)
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=config_main_keyboard(),
        )


async def config_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text input for config value changes (leverage, risk limits)."""
    awaiting = context.user_data.get("awaiting_config")
    if not awaiting:
        return  # Not waiting for config input, ignore

    user_db = _get_user_db(context)
    chat_id = update.effective_chat.id
    user_id = user_db.get_user_by_telegram_chat_id(chat_id)
    if not user_id:
        return

    text = update.message.text.strip()

    # Validate and apply — keep awaiting_config until success
    try:
        if awaiting == "max_leverage":
            value = int(text)
            if not 1 <= value <= 50:
                await update.message.reply_text(
                    "Leverage must be between 1 and 50. Try again:"
                )
                return
            user_db.update_user_config(user_id, max_leverage=value)

        elif awaiting == "max_open_positions":
            value = int(text)
            if not 1 <= value <= 50:
                await update.message.reply_text("Must be between 1 and 50. Try again:")
                return
            user_db.update_user_config(user_id, max_open_positions=value)

        elif awaiting == "max_position_size_usd":
            value = float(text)
            if value < 10:
                await update.message.reply_text("Minimum is $10. Try again:")
                return
            user_db.update_user_config(user_id, max_position_size_usd=value)

        elif awaiting == "max_total_exposure_usd":
            value = float(text)
            if value < 10:
                await update.message.reply_text("Minimum is $10. Try again:")
                return
            user_db.update_user_config(user_id, max_total_exposure_usd=value)

        elif awaiting == "max_daily_loss_pct":
            value = float(text)
            if not 0.1 <= value <= 100:
                await update.message.reply_text("Must be between 0.1% and 100%. Try again:")
                return
            user_db.update_user_config(user_id, max_daily_loss_pct=value)

        else:
            context.user_data.pop("awaiting_config", None)
            await update.message.reply_text("Unknown setting.")
            return

    except ValueError:
        await update.message.reply_text("Invalid number. Try again:")
        return

    # Success — clear state and show updated config with menu
    context.user_data.pop("awaiting_config", None)
    cfg = user_db.get_user_config(user_id)
    config_text = _format_config(cfg)
    await update.message.reply_text(
        config_text + "\n\n_Setting updated._",
        parse_mode="Markdown",
        reply_markup=config_main_keyboard(),
    )
