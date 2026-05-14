"""Telegram command handlers for the 1-Tap Trade onboarding flow.

Commands:

  /connect    Start the delegate-key onboarding (Elite-gated).
  /disconnect Wipe the user's delegate key from the bot.
  /trading    Status: whether connected, last trade, last error.

The /connect flow is a two-step conversation:

  1. /connect              -> bot shows the risk disclosure with an
                              "I understand, continue" callback button.
                              Sets user_data["trading_state"] = "disclosure_shown".

  2. callback "tc:continue" -> bot shows the how-to + how to format the
                              paste-back message, sets state to
                              "awaiting_paste".

  3. plain text reply       -> if state is "awaiting_paste" and message
                              matches the trader/delegate format, parse,
                              validate, encrypt, store, confirm. Clear
                              state. Any non-matching message keeps the
                              state so the user can retry without
                              re-running /connect.

State lives in ``ctx.user_data`` so it is per-user, in-memory, and gone
on process restart. That is acceptable: a restart-mid-onboarding just
means the user has to run /connect again, which is fine for a one-time
flow.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.config import Config
from src.trading.delegates_db import DelegatesDB
from src.trading.disclosure import (
    CONNECT_DISCLOSURE,
    CONNECT_HOWTO,
    DISCONNECT_CONFIRMED,
)
from src.trading.user_settings_db import UserTradingSettingsDB
from src.verification.db import VerificationDB

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


_STATE_KEY = "trading_state"
_STATE_DISCLOSURE = "disclosure_shown"
_STATE_AWAITING = "awaiting_paste"

_CB_CONTINUE = "tc:continue"
_CB_CANCEL = "tc:cancel"


_TRADER_RE = re.compile(
    r"trader\s*[:=]\s*(0x[a-fA-F0-9]{40})", re.IGNORECASE
)
_DELEGATE_RE = re.compile(
    r"delegate\s*[:=]\s*(0x[a-fA-F0-9]{64})", re.IGNORECASE
)


def _parse_paste(text: str) -> tuple[str, str] | None:
    """Pull (trader_address, delegate_private_key) out of a paste-back message.

    Tolerant of: case, key/value separator (: or =), order, surrounding
    whitespace, and extra text the user might leave in by accident. Returns
    None if either field is missing or malformed.
    """
    trader_match = _TRADER_RE.search(text)
    delegate_match = _DELEGATE_RE.search(text)
    if not trader_match or not delegate_match:
        return None
    return trader_match.group(1), delegate_match.group(1)


class TradingCommands:
    """Wires /connect, /disconnect, /trading and their conversation handlers."""

    def __init__(
        self,
        config: Config,
        verification_db: VerificationDB,
        delegates_db: DelegatesDB,
        settings_db: UserTradingSettingsDB,
    ):
        self._config = config
        self._verification_db = verification_db
        self._delegates_db = delegates_db
        self._settings_db = settings_db

    def register(self, application: Application) -> None:
        application.add_handler(CommandHandler("connect", self._cmd_connect))
        application.add_handler(CommandHandler("disconnect", self._cmd_disconnect))
        application.add_handler(CommandHandler("trading", self._cmd_status))
        application.add_handler(
            CallbackQueryHandler(self._cb_continue, pattern=f"^{_CB_CONTINUE}$")
        )
        application.add_handler(
            CallbackQueryHandler(self._cb_cancel, pattern=f"^{_CB_CANCEL}$")
        )
        # Plain-text paste-back handler. Runs LAST (group=99) so it does not
        # capture menu-button taps or other CommandHandlers' input. Only
        # acts when user_data[_STATE_KEY] == _STATE_AWAITING.
        application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._on_text_while_awaiting,
            ),
            group=99,
        )

    async def _is_elite(self, telegram_user_id: int) -> bool:
        record = await self._verification_db.get_verified(telegram_user_id)
        return record is not None and record.is_active

    async def _cmd_connect(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not update.effective_user or not update.effective_message:
            return
        user_id = update.effective_user.id

        if not self._config.trading.enabled:
            await update.effective_message.reply_text(
                "1-Tap Trade is not enabled yet on this bot. "
                "Stay tuned in the Potion announcements."
            )
            return

        if not await self._is_elite(user_id):
            await update.effective_message.reply_text(
                "1-Tap Trade is available to Elite members only. "
                "Run /verify to confirm your Elite role, then run /connect again."
            )
            return

        existing = await self._delegates_db.get(user_id)
        if existing is not None and existing.is_active:
            await update.effective_message.reply_text(
                "You are already connected. Trader address "
                f"<code>{existing.trader_address}</code>.\n\n"
                "Use /disconnect to wipe your key, or just tap "
                "<b>1-Tap Trade</b> on a signal.",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("I understand, continue", callback_data=_CB_CONTINUE)],
            [InlineKeyboardButton("Cancel", callback_data=_CB_CANCEL)],
        ])
        if ctx.user_data is not None:
            ctx.user_data[_STATE_KEY] = _STATE_DISCLOSURE
        await update.effective_message.reply_text(
            CONNECT_DISCLOSURE,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    async def _cb_continue(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        if not query or not update.effective_user:
            return
        await query.answer()

        user_id = update.effective_user.id
        if not await self._is_elite(user_id):
            await query.edit_message_text(
                "Elite verification required. Run /verify first."
            )
            return

        if ctx.user_data is not None:
            ctx.user_data[_STATE_KEY] = _STATE_AWAITING
        await query.edit_message_text(
            CONNECT_HOWTO,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def _cb_cancel(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        if not query:
            return
        await query.answer("Cancelled.")
        if ctx.user_data is not None and _STATE_KEY in ctx.user_data:
            del ctx.user_data[_STATE_KEY]
        await query.edit_message_text(
            "Cancelled. Run /connect again whenever you are ready."
        )

    async def _on_text_while_awaiting(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not update.effective_user or not update.effective_message:
            return
        if ctx.user_data is None:
            return
        if ctx.user_data.get(_STATE_KEY) != _STATE_AWAITING:
            return

        user_id = update.effective_user.id
        text = update.effective_message.text or ""

        parsed = _parse_paste(text)
        if parsed is None:
            await update.effective_message.reply_text(
                "I could not find both a <code>trader:</code> 0x... line "
                "and a <code>delegate:</code> 0x... line in that message. "
                "Try again, or run /disconnect to cancel.",
                parse_mode="HTML",
            )
            return

        trader, delegate_key = parsed

        try:
            await self._delegates_db.upsert(
                telegram_user_id=user_id,
                trader_address=trader,
                delegate_private_key=delegate_key,
            )
        except Exception:
            logger.exception("Failed to store delegate for user %d", user_id)
            await update.effective_message.reply_text(
                "Something went wrong on our side storing your key. "
                "Nothing was saved. Please try /connect again."
            )
            return

        # Clear state, delete the message that contained the key so it
        # does not linger in chat history.
        ctx.user_data.pop(_STATE_KEY, None)
        try:
            await update.effective_message.delete()
        except Exception:
            logger.debug(
                "Could not delete paste-back message for user %d (best-effort)",
                user_id,
            )

        await ctx.bot.send_message(
            chat_id=user_id,
            text=(
                "<b>Connected.</b>\n\n"
                f"Trader: <code>{trader}</code>\n\n"
                "Tap <b>1-Tap Trade</b> on any perps signal to fire a "
                "prefilled trade. You only need to set the size.\n\n"
                "Adjust slippage and size presets anytime with "
                "/settings. Wipe the key from our server with /disconnect."
            ),
            parse_mode="HTML",
        )
        logger.info(
            "Trading delegate stored for telegram_user_id=%d trader=%s",
            user_id, trader,
        )

    async def _cmd_disconnect(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not update.effective_user or not update.effective_message:
            return
        user_id = update.effective_user.id

        existing = await self._delegates_db.get(user_id)
        if existing is None:
            await update.effective_message.reply_text(
                "You are not connected. Nothing to disconnect."
            )
            return

        await self._delegates_db.delete(user_id)
        if ctx.user_data is not None:
            ctx.user_data.pop(_STATE_KEY, None)
        await update.effective_message.reply_text(
            DISCONNECT_CONFIRMED,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info(
            "Trading delegate deleted for telegram_user_id=%d", user_id,
        )

    async def _cmd_status(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not update.effective_user or not update.effective_message:
            return
        user_id = update.effective_user.id

        record = await self._delegates_db.get(user_id)
        if record is None or not record.is_active:
            await update.effective_message.reply_text(
                "1-Tap Trade: <b>not connected</b>.\n\n"
                "Run /connect to set it up (Elite only).",
                parse_mode="HTML",
            )
            return

        settings = await self._settings_db.get_or_default(user_id)
        connected_at = datetime.fromtimestamp(record.connected_at, tz=timezone.utc)
        last_trade = (
            datetime.fromtimestamp(record.last_trade_at, tz=timezone.utc)
            if record.last_trade_at
            else None
        )
        lines = [
            "1-Tap Trade: <b>connected</b>",
            "",
            f"Trader: <code>{record.trader_address}</code>",
            f"Connected: {connected_at:%Y-%m-%d %H:%M UTC}",
        ]
        if last_trade is not None:
            lines.append(f"Last trade: {last_trade:%Y-%m-%d %H:%M UTC}")
        if record.last_failure_at and record.last_failure_reason:
            failed_at = datetime.fromtimestamp(
                record.last_failure_at, tz=timezone.utc,
            )
            lines.append("")
            lines.append(
                f"Last failure ({failed_at:%Y-%m-%d %H:%M UTC}):"
            )
            lines.append(f"<i>{record.last_failure_reason}</i>")
        lines.append("")
        lines.append(
            f"Slippage: {settings.slippage_bps / 100:.2f}%   "
            f"Presets: {', '.join(f'${int(p)}' for p in settings.size_presets)}"
        )
        lines.append("")
        lines.append("Use /settings to change slippage or presets, "
                     "/disconnect to wipe your key.")

        await update.effective_message.reply_text(
            "\n".join(lines),
            parse_mode="HTML",
        )
