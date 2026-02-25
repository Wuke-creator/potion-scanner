"""Trade notification sender for Telegram.

Sends push notifications to users when trade events occur (new signals,
fills, TP hits, stop losses, closures, breakeven moves). All methods are
fire-and-forget — notification failures are logged but never crash the pipeline.
"""

import asyncio
import logging
from typing import Any, Callable

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from src.state.user_db import UserDatabase
from src.telegram.formatters import format_pct, format_usd

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends trade-lifecycle notifications to a single user's Telegram chat.

    Each Pipeline instance gets its own TelegramNotifier scoped to one user.
    If no telegram_chat_id is found for the user, all methods silently skip.
    """

    def __init__(
        self,
        bot: Bot,
        user_db: UserDatabase,
        user_id: str,
        calls_view_checker: Callable[[], bool] | None = None,
        calls_view_refresher: Callable[[], Any] | None = None,
    ) -> None:
        self._bot = bot
        self._user_db = user_db
        self._user_id = user_id
        self._chat_id: int | None = user_db.get_telegram_chat_id(user_id)
        self._calls_view_checker = calls_view_checker
        self._calls_view_refresher = calls_view_refresher

    def _get_chat_id(self) -> int | None:
        """Return cached chat_id, refreshing once if initially None."""
        if self._chat_id is None:
            self._chat_id = self._user_db.get_telegram_chat_id(self._user_id)
        return self._chat_id

    async def _send(self, text: str, reply_markup: Any = None) -> None:
        """Send a message to the user's chat. Silently handles all errors."""
        chat_id = self._get_chat_id()
        if chat_id is None:
            logger.debug("_send: no chat_id for user %s, skipping", self._user_id)
            return

        logger.debug("_send: sending to chat_id=%s for user %s", chat_id, self._user_id)
        try:
            await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
            logger.debug("_send: message sent successfully to user %s", self._user_id)
        except Exception:
            logger.exception("Failed to send notification to user %s", self._user_id)

    # ------------------------------------------------------------------
    # Notification methods
    # ------------------------------------------------------------------

    async def notify_signal_skipped(self, trade_id: int, pair: str, reason: str) -> None:
        """Signal received but could not be executed."""
        text = (
            f"⏭ *Signal Skipped — #{trade_id}*\n\n"
            f"💱 Pair: {pair}\n"
            f"⚠️ Reason: {reason}"
        )
        await self._send(text)

    async def notify_new_signal(
        self,
        signal: Any,
        trade_set: Any,
        position_size_usd: float,
        auto_execute: bool = True,
        warning: str | None = None,
    ) -> None:
        """New signal received — notify user with trade details.

        Gating logic:
        - auto_execute=True  → always send (informational, no buttons)
        - auto_execute=False → only send if user is in Calls View
          (otherwise the signal stays PENDING in the DB)
        """
        # Gate: when manual approval is needed, only notify if user is in calls view
        if not auto_execute:
            in_calls = (
                self._calls_view_checker()
                if self._calls_view_checker is not None
                else False
            )
            logger.debug(
                "notify_new_signal: user=%s auto_execute=%s in_calls=%s checker=%s",
                self._user_id, auto_execute, in_calls,
                self._calls_view_checker is not None,
            )
            if not in_calls:
                logger.debug(
                    "Skipping new-signal notification for user %s (not in calls view)",
                    self._user_id,
                )
                return

        side_emoji = "📈 LONG" if signal.side.value.upper() == "LONG" else "📉 SHORT"
        text = (
            f"🔔 *New Signal — Trade #{signal.trade_id}*\n\n"
            f"💱 Pair: {signal.pair}\n"
            f"Direction: {side_emoji}\n"
            f"🎯 Entry: {signal.entry}\n"
            f"🛡 Stop Loss: {signal.stop_loss}\n"
            f"💰 Size: {format_usd(position_size_usd)}\n"
            f"📊 Leverage: {trade_set.leverage}x"
        )

        if warning:
            text += f"\n\n⚠️ {warning}"

        reply_markup = None
        if not auto_execute:
            text += "\n\n_⚡ Auto-execute is OFF. Approve or reject this trade:_"
            reply_markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"signal:approve:{signal.trade_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"signal:reject:{signal.trade_id}"),
                ]
            ])

        await self._send(text, reply_markup=reply_markup)

        # Also refresh the calls view so it shows the new signal inline
        if not auto_execute and self._calls_view_refresher is not None:
            try:
                await self._calls_view_refresher()
            except Exception:
                logger.exception(
                    "Failed to refresh calls view for user %s", self._user_id,
                )

    async def notify_trade_opened(
        self, trade_id: int, coin: str, side: str, entry_price: float, size_usd: float,
    ) -> None:
        """Entry order filled — trade is now open."""
        side_emoji = "📈" if side.upper() == "LONG" else "📉"
        text = (
            f"✅ *Trade Opened — #{trade_id}*\n\n"
            f"💱 Coin: {coin}\n"
            f"{side_emoji} Side: {side.upper()}\n"
            f"🎯 Entry: {entry_price}\n"
            f"💰 Size: {format_usd(size_usd)}"
        )
        await self._send(text)

    async def notify_trade_failed(self, trade_id: int, coin: str, error: str) -> None:
        """Trade submission failed."""
        text = (
            f"❌ *Trade Failed — #{trade_id}*\n\n"
            f"💱 Coin: {coin}\n"
            f"⚠️ Error: {error}"
        )
        await self._send(text)

    async def notify_tp_hit(
        self, trade_id: int, coin: str, tp_number: int, profit_pct: float,
    ) -> None:
        """Partial take-profit hit."""
        text = (
            f"🎯 *TP{tp_number} Hit — Trade #{trade_id}*\n\n"
            f"💱 Coin: {coin}\n"
            f"💹 Profit: {format_pct(profit_pct)}"
        )
        await self._send(text)

    async def notify_all_tp_hit(
        self, trade_id: int, coin: str, profit_pct: float,
    ) -> None:
        """All TPs hit — trade fully closed in profit."""
        text = (
            f"🏆 *All TPs Hit — Trade #{trade_id}*\n\n"
            f"💱 Coin: {coin}\n"
            f"💹 Total Profit: {format_pct(profit_pct)}\n"
            f"✅ Trade fully closed."
        )
        await self._send(text)

    async def notify_stop_hit(
        self, trade_id: int, coin: str, loss_pct: float,
    ) -> None:
        """Stop loss hit — trade closed at a loss."""
        text = (
            f"🛑 *Stop Hit — Trade #{trade_id}*\n\n"
            f"💱 Coin: {coin}\n"
            f"📉 Loss: {format_pct(loss_pct)}"
        )
        await self._send(text)

    async def notify_trade_canceled(
        self, trade_id: int, coin: str, reason: str,
    ) -> None:
        """Trade canceled."""
        text = (
            f"🚫 *Trade Canceled — #{trade_id}*\n\n"
            f"💱 Coin: {coin}\n"
            f"📋 Reason: {reason}"
        )
        await self._send(text)

    async def notify_trade_closed(
        self, trade_id: int, coin: str, detail: str,
    ) -> None:
        """Trade manually closed."""
        text = (
            f"📋 *Trade Closed — #{trade_id}*\n\n"
            f"💱 Coin: {coin}\n"
            f"📝 Detail: {detail}"
        )
        await self._send(text)

    async def notify_breakeven(
        self, trade_id: int, coin: str, entry_price: float,
    ) -> None:
        """Stop loss moved to breakeven (entry price)."""
        text = (
            f"⚖️ *Breakeven — Trade #{trade_id}*\n\n"
            f"💱 Coin: {coin}\n"
            f"🛡 SL moved to entry: {entry_price}"
        )
        await self._send(text)

    async def notify_sl_moved(
        self, trade_id: int, coin: str, new_price: float,
    ) -> None:
        """Stop loss adjusted to a new price."""
        text = (
            f"🛡 *SL Moved — Trade #{trade_id}*\n\n"
            f"💱 Coin: {coin}\n"
            f"🎯 New SL: {new_price}"
        )
        await self._send(text)

    async def notify_pnl_alert(self, coin: str, side: str, pnl_pct: float, threshold_type: str) -> None:
        """PnL threshold alert — profit or loss."""
        emoji = "💹" if threshold_type == "profit" else "📉"
        text = (
            f"{emoji} *PnL Alert — {coin} {side}*\n\n"
            f"Unrealized PnL: {pnl_pct:+.2f}%"
        )
        await self._send(text)

    async def notify_risk_warning(self, message: str) -> None:
        """Risk limit warning."""
        text = f"⚠️ *Risk Warning*\n\n{message}"
        await self._send(text)
