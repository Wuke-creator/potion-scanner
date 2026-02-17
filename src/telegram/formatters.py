"""Message formatting utilities for Telegram bot responses."""

from typing import Any


def mask_address(address: str) -> str:
    """Show first 6 and last 4 chars of an address: 0x1234...abcd."""
    if len(address) <= 10:
        return address
    return f"{address[:6]}...{address[-4:]}"


def format_expiry(expires_at: str | None) -> str:
    """Format access expiry for display."""
    if expires_at is None:
        return "Unlimited"
    return expires_at[:10]


def format_usd(value: str | float) -> str:
    """Format a USD value: $1,234.56."""
    v = float(value)
    return f"${v:,.2f}"


def format_pnl(value: str | float) -> str:
    """Format PnL with sign: +$123.45 or -$67.89."""
    v = float(value)
    sign = "+" if v >= 0 else ""
    return f"{sign}${v:,.2f}"


def format_pct(value: float) -> str:
    """Format percentage with sign: +12.34% or -5.67%."""
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def format_balance(balance: dict[str, str]) -> str:
    """Format balance data into a display string."""
    return (
        "💰 *Account Balance*\n\n"
        f"💵 USDC Balance: {format_usd(balance['usdc_balance'])}\n"
        f"📊 Account Value: {format_usd(balance['account_value'])}\n"
        f"📋 Margin Used: {format_usd(balance['total_margin_used'])}\n"
        f"📂 Position Value: {format_usd(balance['total_position_value'])}\n"
        f"💸 Withdrawable: {format_usd(balance['withdrawable'])}"
    )


def format_positions(positions: list[dict[str, Any]]) -> str:
    """Format open positions into a display string."""
    if not positions:
        return "📂 *Open Positions*\n\nNo open positions."

    lines = ["📂 *Open Positions*\n"]
    for p in positions:
        side = "LONG" if p["size"] > 0 else "SHORT"
        direction = "📈" if side == "LONG" else "📉"
        size = abs(p["size"])
        pnl = format_pnl(p["unrealized_pnl"])
        lines.append(
            f"{direction} *{p['coin']}* {side}\n"
            f"  Size: {size} | Entry: {p['entry_price']}\n"
            f"  PnL: {pnl} | Lev: {p['leverage']}x\n"
            f"  Liq: {p['liquidation_price']}"
        )

    return "\n\n".join(lines)


def format_status(
    user_config: dict,
    balance: dict[str, str],
    positions: list,
    expires_at: str | None,
) -> str:
    """Format risk dashboard / status view."""
    open_count = len(positions)
    max_pos = user_config.get("max_open_positions", 10)

    total_exposure = sum(
        abs(float(p.get("size", 0)) * float(p.get("entry_price", 0)))
        for p in positions
    )
    max_exposure = user_config.get("max_total_exposure_usd", 2000)

    return (
        "🛡 *Risk Dashboard*\n\n"
        f"🎯 Preset: {user_config.get('active_preset', 'runner')}\n"
        f"⚡ Auto-execute: {'ON' if user_config.get('auto_execute') else 'OFF'}\n"
        f"📊 Max Leverage: {user_config.get('max_leverage', 20)}x\n\n"
        f"🔒 *Risk Limits*\n"
        f"📊 Positions: {open_count}/{max_pos}\n"
        f"📈 Exposure: {format_usd(total_exposure)} / {format_usd(max_exposure)}\n"
        f"💰 Max Position: {format_usd(user_config.get('max_position_size_usd', 500))}\n"
        f"🛡 Daily Loss Limit: {user_config.get('max_daily_loss_pct', 10)}%\n\n"
        f"💼 *Account*\n"
        f"💵 Balance: {format_usd(balance.get('account_value', '0'))}\n"
        f"⏰ Access Expires: {format_expiry(expires_at)}"
    )


def format_account_info(
    user_config: dict,
    credentials: dict,
    expires_at: str | None,
) -> str:
    """Format account & membership info for the account submenu."""
    wallet = mask_address(credentials.get("account_address", "N/A"))
    api_wallet = mask_address(credentials.get("api_wallet", "N/A"))
    network = credentials.get("network", "testnet").capitalize()
    invite_code = user_config.get("invite_code", "N/A")

    return (
        "👤 *Account & Membership*\n\n"
        f"💼 Wallet: `{wallet}`\n"
        f"🔑 API Wallet: `{api_wallet}`\n"
        f"🌐 Network: {network}\n\n"
        f"📋 Subscription: Active\n"
        f"⏰ Expires: {format_expiry(expires_at)}\n"
        f"🎟 Code Used: {invite_code}"
    )


def _status_badge(status_value: str) -> str:
    """Return an emoji + label for a trade status."""
    mapping = {
        "open": ("✅", "TAKEN"),
        "closed": ("🏁", "CLOSED"),
        "canceled": ("❌", "PASSED"),
    }
    icon, label = mapping.get(status_value, ("⏳", "PENDING"))
    return f"{icon} {label}"


def _format_signal_card(t: Any) -> str:
    """Render one trade as a full signal card (matches provider format)."""
    status = t.status.value if hasattr(t.status, "value") else str(t.status)
    badge = _status_badge(status)
    side_emoji = "📈" if t.side.upper() == "LONG" else "📉"
    risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(t.risk_level.upper(), "⚪")

    # SL distance %
    if t.entry_price:
        sl_pct = ((t.stop_loss - t.entry_price) / t.entry_price) * 100
    else:
        sl_pct = 0.0

    # TP distances %
    tp_pcts = []
    for tp_val in (t.tp1, t.tp2, t.tp3):
        if t.entry_price:
            tp_pcts.append(((tp_val - t.entry_price) / t.entry_price) * 100)
        else:
            tp_pcts.append(0.0)

    return (
        f"{'─' * 28}\n"
        f"🔔 *SIGNAL #{t.trade_id}*  —  {badge}\n\n"
        f"💱 Pair: `{t.pair}`\n"
        f"{risk_emoji} Risk: {t.risk_level.upper()}\n"
        f"📋 Type: {t.trade_type.upper()}  |  Size: {t.size_hint}\n"
        f"{side_emoji} Side: *{t.side.upper()}*\n\n"
        f"🎯 Entry: `{t.entry_price}`\n"
        f"🛡 SL: `{t.stop_loss}`  ({sl_pct:+.2f}%)\n\n"
        f"🏆 *Take Profit Targets:*\n"
        f"  TP1: `{t.tp1}`  ({tp_pcts[0]:+.2f}%)\n"
        f"  TP2: `{t.tp2}`  ({tp_pcts[1]:+.2f}%)\n"
        f"  TP3: `{t.tp3}`  ({tp_pcts[2]:+.2f}%)\n\n"
        f"📊 Leverage: {t.leverage}x"
    )


def format_calls_view(trades: list) -> str:
    """Format recent trades for the calls view with full signal details."""
    if not trades:
        return (
            "📡 *Calls View*\n\n"
            "_Signal approval mode — incoming signals appear here._\n\n"
            "No recent signals."
        )

    header = (
        "📡 *Calls View*\n\n"
        "_Signal approval mode — approve or reject incoming trades._"
    )
    cards = [_format_signal_card(t) for t in trades]
    return header + "\n\n" + "\n\n".join(cards)


def format_trading_hub(balance: dict[str, str] | None, positions: list | None, open_trades: int = 0) -> str:
    """Format the trading hub summary."""
    text = "📊 *Trading*\n\n"

    if balance:
        text += f"💰 Balance: {format_usd(balance.get('account_value', '0'))}\n"
    else:
        text += "💰 Balance: _Unavailable_\n"

    # Calculate unrealized PnL from positions
    if positions:
        total_unrealized = sum(float(p.get("unrealized_pnl", 0)) for p in positions)
        text += f"📈 Unrealized PnL: {format_pnl(total_unrealized)}\n"

    pos_count = len(positions) if positions else 0
    text += f"📂 Open Positions: {pos_count}\n"
    text += f"📋 Active Trades: {open_trades}"

    return text


def format_stats(closed_trades: list, open_count: int) -> str:
    """Format trading statistics."""
    if not closed_trades and open_count == 0:
        return "📈 *Trading Statistics*\n\nNo trades yet."

    total = len(closed_trades)
    pnls = [t.pnl_pct for t in closed_trades if t.pnl_pct is not None]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    breakeven = sum(1 for p in pnls if p == 0)

    total_pnl = sum(pnls) if pnls else 0
    avg_pnl = total_pnl / len(pnls) if pnls else 0
    best = max(pnls) if pnls else 0
    worst = min(pnls) if pnls else 0
    win_rate = (wins / total * 100) if total > 0 else 0

    return (
        "📈 *Trading Statistics*\n\n"
        "📊 *Overview*\n"
        f"Total Closed: {total}\n"
        f"Currently Open: {open_count}\n"
        f"Win Rate: {win_rate:.1f}%\n\n"
        "💹 *Results*\n"
        f"Wins: {wins} | Losses: {losses} | BE: {breakeven}\n"
        f"Total PnL: {'+' if total_pnl >= 0 else ''}{total_pnl:.2f}%\n"
        f"Avg: {'+' if avg_pnl >= 0 else ''}{avg_pnl:.2f}% | "
        f"Best: {'+' if best >= 0 else ''}{best:.2f}%\n"
        f"Worst: {'+' if worst >= 0 else ''}{worst:.2f}%"
    )


def format_dashboard(
    user_config: dict,
    is_active: bool,
    expires_at: str | None,
) -> str:
    """Format the risk dashboard for the menu view."""
    from src.config.settings import BUILTIN_PRESETS

    preset_name = user_config.get("active_preset", "runner")
    p = BUILTIN_PRESETS.get(preset_name)
    tp_desc = ""
    if p:
        tp_pcts = [int(x * 100) for x in p.tp_split]
        tp_desc = f" ({tp_pcts[0]}/{tp_pcts[1]}/{tp_pcts[2]})"

    auto = "✅ Auto" if user_config.get("auto_execute") else "👋 Manual Approve"
    pipeline = "▶️ Active" if is_active else "⏸ Paused"

    return (
        "🛡 *Risk Dashboard*\n\n"
        f"▶️ Pipeline: {pipeline}\n"
        f"🤖 Calls Mode: {auto}\n"
        f"🎯 Strategy: {preset_name}{tp_desc}\n"
        f"📊 Leverage: {user_config.get('max_leverage', 20)}x\n\n"
        "🔒 *Risk Limits*\n"
        f"Max Positions: {user_config.get('max_open_positions', 10)}\n"
        f"Max Position: {format_usd(user_config.get('max_position_size_usd', 500))}\n"
        f"Max Exposure: {format_usd(user_config.get('max_total_exposure_usd', 2000))}\n"
        f"Daily Loss Limit: {user_config.get('max_daily_loss_pct', 10)}%\n\n"
        f"⏰ Access Expires: {format_expiry(expires_at)}"
    )
