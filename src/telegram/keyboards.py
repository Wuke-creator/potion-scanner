"""Inline keyboard builders for Telegram bot."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def account_nav_keyboard(current: str = "balance") -> InlineKeyboardMarkup:
    """Navigation keyboard for account views (balance, positions, status)."""
    buttons = []
    if current != "balance":
        buttons.append(InlineKeyboardButton("Balance", callback_data="nav:balance"))
    if current != "positions":
        buttons.append(InlineKeyboardButton("Positions", callback_data="nav:positions"))
    if current != "status":
        buttons.append(InlineKeyboardButton("Status", callback_data="nav:status"))
    return InlineKeyboardMarkup([buttons])


def config_main_keyboard() -> InlineKeyboardMarkup:
    """Main config menu keyboard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Strategy", callback_data="cfg:strategy"),
            InlineKeyboardButton("Auto-Execute", callback_data="cfg:auto"),
        ],
        [
            InlineKeyboardButton("Risk Limits", callback_data="cfg:risk"),
            InlineKeyboardButton("Leverage", callback_data="cfg:leverage"),
        ],
    ])


def preset_keyboard() -> InlineKeyboardMarkup:
    """Strategy preset selection keyboard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("runner", callback_data="cfg:preset:runner"),
            InlineKeyboardButton("conservative", callback_data="cfg:preset:conservative"),
        ],
        [
            InlineKeyboardButton("tp2_exit", callback_data="cfg:preset:tp2_exit"),
            InlineKeyboardButton("tp3_hold", callback_data="cfg:preset:tp3_hold"),
        ],
        [
            InlineKeyboardButton("breakeven_filter", callback_data="cfg:preset:breakeven_filter"),
            InlineKeyboardButton("small_runner", callback_data="cfg:preset:small_runner"),
        ],
        [InlineKeyboardButton("< Back", callback_data="cfg:back")],
    ])


def risk_keyboard() -> InlineKeyboardMarkup:
    """Risk limits adjustment keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Max Positions", callback_data="cfg:risk:max_open_positions")],
        [InlineKeyboardButton("Max Position Size", callback_data="cfg:risk:max_position_size_usd")],
        [InlineKeyboardButton("Max Exposure", callback_data="cfg:risk:max_total_exposure_usd")],
        [InlineKeyboardButton("Daily Loss Limit", callback_data="cfg:risk:max_daily_loss_pct")],
        [InlineKeyboardButton("< Back", callback_data="cfg:back")],
    ])
