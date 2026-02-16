"""Inline keyboard builders for Telegram bot."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# ------------------------------------------------------------------
# Navigation helpers
# ------------------------------------------------------------------

def _back_refresh_close(back_target: str, back_label: str = "⬅️ Menu") -> list[list[InlineKeyboardButton]]:
    """Standard navigation rows: [Back | Refresh] + [Close]."""
    return [
        [
            InlineKeyboardButton(back_label, callback_data=back_target),
            InlineKeyboardButton("🔄 Refresh", callback_data="menu:refresh"),
        ],
        [InlineKeyboardButton("✖ Close", callback_data="menu:close")],
    ]


# ------------------------------------------------------------------
# Main menu
# ------------------------------------------------------------------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Main menu — 3 rows of 2 buttons + Close."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👤 Account", callback_data="menu:account"),
            InlineKeyboardButton("📡 Calls View", callback_data="menu:calls"),
        ],
        [
            InlineKeyboardButton("📊 Trading", callback_data="menu:trading"),
            InlineKeyboardButton("📈 Statistics", callback_data="menu:stats"),
        ],
        [
            InlineKeyboardButton("🛡 Dashboard", callback_data="menu:dashboard"),
            InlineKeyboardButton("⚙️ Configuration", callback_data="menu:config"),
        ],
        [InlineKeyboardButton("✖ Close", callback_data="menu:close")],
    ])


# ------------------------------------------------------------------
# Submenu keyboards
# ------------------------------------------------------------------

def account_keyboard() -> InlineKeyboardMarkup:
    """Account submenu — read-only, just nav."""
    return InlineKeyboardMarkup(_back_refresh_close("menu:main"))


def calls_view_keyboard() -> InlineKeyboardMarkup:
    """Calls view — Back + Refresh + Close."""
    return InlineKeyboardMarkup(_back_refresh_close("menu:main"))


def trading_hub_keyboard() -> InlineKeyboardMarkup:
    """Trading hub — 4 sub-buttons + nav."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Balance", callback_data="trading:balance"),
            InlineKeyboardButton("📂 Positions", callback_data="trading:positions"),
        ],
        [
            InlineKeyboardButton("📋 Trades", callback_data="trading:trades"),
            InlineKeyboardButton("📜 History", callback_data="trading:history"),
        ],
        *_back_refresh_close("menu:main"),
    ])


def trading_sub_keyboard() -> InlineKeyboardMarkup:
    """Trading sub-view (balance, positions, etc.) — Back to Trading + nav."""
    return InlineKeyboardMarkup(_back_refresh_close("menu:trading", "⬅️ Trading"))


def stats_keyboard() -> InlineKeyboardMarkup:
    """Statistics view — Back + Refresh + Close."""
    return InlineKeyboardMarkup(_back_refresh_close("menu:main"))


def dashboard_keyboard() -> InlineKeyboardMarkup:
    """Risk Dashboard — Back + Refresh + Close."""
    return InlineKeyboardMarkup(_back_refresh_close("menu:main"))


def config_menu_keyboard(is_active: bool = True) -> InlineKeyboardMarkup:
    """Configuration submenu — settings buttons + activate/deactivate + nav."""
    toggle_btn = (
        InlineKeyboardButton("⏸ Deactivate", callback_data="cfg:deactivate")
        if is_active
        else InlineKeyboardButton("▶️ Activate", callback_data="cfg:activate")
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎯 Preset", callback_data="cfg:strategy"),
            InlineKeyboardButton("⚡ Auto-Execute", callback_data="cfg:auto"),
        ],
        [
            InlineKeyboardButton("📊 Leverage", callback_data="cfg:leverage"),
            InlineKeyboardButton("🛡 Risk Limits", callback_data="cfg:risk"),
        ],
        [toggle_btn],
        *_back_refresh_close("menu:main"),
    ])


def preset_keyboard() -> InlineKeyboardMarkup:
    """Strategy preset selection keyboard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎯 runner", callback_data="cfg:preset:runner"),
            InlineKeyboardButton("🛡 conservative", callback_data="cfg:preset:conservative"),
        ],
        [
            InlineKeyboardButton("📊 tp2_exit", callback_data="cfg:preset:tp2_exit"),
            InlineKeyboardButton("📈 tp3_hold", callback_data="cfg:preset:tp3_hold"),
        ],
        [
            InlineKeyboardButton("⚖️ breakeven_filter", callback_data="cfg:preset:breakeven_filter"),
            InlineKeyboardButton("🏃 small_runner", callback_data="cfg:preset:small_runner"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="cfg:back")],
    ])


def risk_keyboard() -> InlineKeyboardMarkup:
    """Risk limits adjustment keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Max Positions", callback_data="cfg:risk:max_open_positions")],
        [InlineKeyboardButton("💰 Max Position Size", callback_data="cfg:risk:max_position_size_usd")],
        [InlineKeyboardButton("📈 Max Exposure", callback_data="cfg:risk:max_total_exposure_usd")],
        [InlineKeyboardButton("🛡 Daily Loss Limit", callback_data="cfg:risk:max_daily_loss_pct")],
        [InlineKeyboardButton("⬅️ Back", callback_data="cfg:back")],
    ])


# ------------------------------------------------------------------
# Legacy (kept for backward compatibility in account nav callbacks)
# ------------------------------------------------------------------

def account_nav_keyboard(current: str = "balance") -> InlineKeyboardMarkup:
    """Navigation keyboard for account views (balance, positions, status)."""
    buttons = []
    if current != "balance":
        buttons.append(InlineKeyboardButton("💰 Balance", callback_data="nav:balance"))
    if current != "positions":
        buttons.append(InlineKeyboardButton("📂 Positions", callback_data="nav:positions"))
    if current != "status":
        buttons.append(InlineKeyboardButton("🛡 Status", callback_data="nav:status"))
    return InlineKeyboardMarkup([buttons])
