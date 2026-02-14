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
        "*Account Balance*\n\n"
        f"USDC Balance: {format_usd(balance['usdc_balance'])}\n"
        f"Account Value: {format_usd(balance['account_value'])}\n"
        f"Margin Used: {format_usd(balance['total_margin_used'])}\n"
        f"Position Value: {format_usd(balance['total_position_value'])}\n"
        f"Withdrawable: {format_usd(balance['withdrawable'])}"
    )


def format_positions(positions: list[dict[str, Any]]) -> str:
    """Format open positions into a display string."""
    if not positions:
        return "*Open Positions*\n\nNo open positions."

    lines = ["*Open Positions*\n"]
    for p in positions:
        side = "LONG" if p["size"] > 0 else "SHORT"
        size = abs(p["size"])
        pnl = format_pnl(p["unrealized_pnl"])
        lines.append(
            f"*{p['coin']}* {side}\n"
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
        "*Status Dashboard*\n\n"
        f"Preset: {user_config.get('active_preset', 'runner')}\n"
        f"Auto-execute: {'ON' if user_config.get('auto_execute') else 'OFF'}\n"
        f"Max Leverage: {user_config.get('max_leverage', 20)}x\n\n"
        f"*Risk*\n"
        f"Positions: {open_count}/{max_pos}\n"
        f"Exposure: {format_usd(total_exposure)} / {format_usd(max_exposure)}\n"
        f"Max Position: {format_usd(user_config.get('max_position_size_usd', 500))}\n"
        f"Daily Loss Limit: {user_config.get('max_daily_loss_pct', 10)}%\n\n"
        f"*Account*\n"
        f"Balance: {format_usd(balance.get('account_value', '0'))}\n"
        f"Access Expires: {format_expiry(expires_at)}"
    )
