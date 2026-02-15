"""Trade notification sender for Telegram.

Sends push notifications to users when trade events occur (new signals,
fills, TP hits, stop losses, closures, breakeven moves). All methods are
fire-and-forget — notification failures are logged but never crash the pipeline.
"""

import asyncio
import logging
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from src.state.user_db import UserDatabase
from src.telegram.formatters import format_pct, format_usd

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends trade-lifecycle notifications to a single user's Telegram chat.

    Each Pipeline instance gets its own TelegramNotifier scoped to one user.
    If no telegram_chat_id is found for the user, all methods silently skip.
    """

    def __init__(self, bot: Bot, user_db: UserDatabase, user_id: str) -> None:
        self._bot = bot
        self._user_db = user_db
        self._user_id = user_id
        self._chat_id: int | None = user_db.get_telegram_chat_id(user_id)

    def _get_chat_id(self) -> int | None:
        """Return cached chat_id, refreshing once if initially None."""
        if self._chat_id is None:
            self._chat_id = self._user_db.get_telegram_chat_id(self._user_id)
        return self._chat_id

    async def _send(self, text: str, reply_markup: Any = None) -> None:
        """Send a message to the user's chat. Silently handles all errors."""
        chat_id = self._get_chat_id()
        if chat_id is None:
            return

        try:
            await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
        except Exception:
            logger.exception("Failed to send notification to user %s", self._user_id)

    # ------------------------------------------------------------------
    # Notification methods
    # ------------------------------------------------------------------

    async def notify_new_signal(
        self,
        signal: Any,
        trade_set: Any,
        position_size_usd: float,
        auto_execute: bool = True,
    ) -> None:
        """New signal received — notify user with trade details."""
        side_emoji = "LONG" if signal.side.value == "long" else "SHORT"
        text = (
            f"*New Signal — Trade #{signal.trade_id}*\n\n"
            f"Pair: {signal.pair}\n"
            f"Side: {side_emoji}\n"
            f"Entry: {signal.entry}\n"
            f"Stop Loss: {signal.stop_loss}\n"
            f"Size: {format_usd(position_size_usd)}\n"
            f"Leverage: {trade_set.leverage}x"
        )

        reply_markup = None
        if not auto_execute:
            text += "\n\n_Auto-execute is OFF. Approve or reject this trade:_"
            reply_markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Approve", callback_data=f"signal:approve:{signal.trade_id}"),
                    InlineKeyboardButton("Reject", callback_data=f"signal:reject:{signal.trade_id}"),
                ]
            ])

        await self._send(text, reply_markup=reply_markup)

    async def notify_trade_opened(
        self, trade_id: int, coin: str, side: str, entry_price: float, size_usd: float,
    ) -> None:
        """Entry order filled — trade is now open."""
        text = (
            f"*Trade Opened — #{trade_id}*\n\n"
            f"Coin: {coin}\n"
            f"Side: {side.upper()}\n"
            f"Entry: {entry_price}\n"
            f"Size: {format_usd(size_usd)}"
        )
        await self._send(text)

    async def notify_trade_failed(self, trade_id: int, coin: str, error: str) -> None:
        """Trade submission failed."""
        text = (
            f"*Trade Failed — #{trade_id}*\n\n"
            f"Coin: {coin}\n"
            f"Error: {error}"
        )
        await self._send(text)

    async def notify_tp_hit(
        self, trade_id: int, coin: str, tp_number: int, profit_pct: float,
    ) -> None:
        """Partial take-profit hit."""
        text = (
            f"*TP{tp_number} Hit — Trade #{trade_id}*\n\n"
            f"Coin: {coin}\n"
            f"Profit: {format_pct(profit_pct)}"
        )
        await self._send(text)

    async def notify_all_tp_hit(
        self, trade_id: int, coin: str, profit_pct: float,
    ) -> None:
        """All TPs hit — trade fully closed in profit."""
        text = (
            f"*All TPs Hit — Trade #{trade_id}*\n\n"
            f"Coin: {coin}\n"
            f"Total Profit: {format_pct(profit_pct)}\n"
            f"Trade fully closed."
        )
        await self._send(text)

    async def notify_stop_hit(
        self, trade_id: int, coin: str, loss_pct: float,
    ) -> None:
        """Stop loss hit — trade closed at a loss."""
        text = (
            f"*Stop Hit — Trade #{trade_id}*\n\n"
            f"Coin: {coin}\n"
            f"Loss: {format_pct(loss_pct)}"
        )
        await self._send(text)

    async def notify_trade_canceled(
        self, trade_id: int, coin: str, reason: str,
    ) -> None:
        """Trade canceled."""
        text = (
            f"*Trade Canceled — #{trade_id}*\n\n"
            f"Coin: {coin}\n"
            f"Reason: {reason}"
        )
        await self._send(text)

    async def notify_trade_closed(
        self, trade_id: int, coin: str, detail: str,
    ) -> None:
        """Trade manually closed."""
        text = (
            f"*Trade Closed — #{trade_id}*\n\n"
            f"Coin: {coin}\n"
            f"Detail: {detail}"
        )
        await self._send(text)

    async def notify_breakeven(
        self, trade_id: int, coin: str, entry_price: float,
    ) -> None:
        """Stop loss moved to breakeven (entry price)."""
        text = (
            f"*Breakeven — Trade #{trade_id}*\n\n"
            f"Coin: {coin}\n"
            f"SL moved to entry: {entry_price}"
        )
        await self._send(text)

    async def notify_sl_moved(
        self, trade_id: int, coin: str, new_price: float,
    ) -> None:
        """Stop loss adjusted to a new price."""
        text = (
            f"*SL Moved — Trade #{trade_id}*\n\n"
            f"Coin: {coin}\n"
            f"New SL: {new_price}"
        )
        await self._send(text)

    async def notify_risk_warning(self, message: str) -> None:
        """Risk limit warning."""
        text = f"*Risk Warning*\n\n{message}"
        await self._send(text)
