"""1-Tap Trade callback flow.

Three callback prefixes wire the user's experience together:

  qt:open:{signal_id}   Initial "1-Tap Trade" button on a signal alert.
                        Triggers Elite/onboarding/Trading-enabled gate
                        checks, then shows the confirmation card with
                        size-preset buttons.

  qt:size:{size_usdc}   Tap on a preset size. Builds the final confirm
                        screen. user_data carries the pending signal_id.

  qt:custom             Tap on "Custom size". Switches user_data to
                        ``awaiting_size`` so the next plain-text reply
                        is treated as the USDC amount.

  qt:confirm            Final submit. Pulls signal + delegate + settings
                        + size from user_data, decrypts the delegate
                        key, calls the trade-executor, edits the message
                        with tx hash or error.

  qt:cancel             Wipe user_data state, edit message to "Cancelled".

State lives in ctx.user_data:

  user_data["trade"] = {
    "signal_id": int,
    "size": float | None,
    "awaiting_size": bool,
  }
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.automations.open_signals_db import OpenSignal, OpenSignalsDB
from src.config import Config
from src.trading.delegates_db import DelegatesDB
from src.trading.executor_client import ExecutorError, TradeExecutorClient
from src.trading.user_settings_db import UserTradingSettingsDB
from src.verification.db import VerificationDB

logger = logging.getLogger(__name__)


_CB_OPEN_PREFIX = "qt:open:"
_CB_SIZE_PREFIX = "qt:size:"
_CB_CUSTOM = "qt:custom"
_CB_CONFIRM = "qt:confirm"
_CB_CANCEL = "qt:cancel"

_USER_DATA_KEY = "trade"
_MAX_SIGNAL_AGE_SEC = 24 * 3600


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 100:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:,.4f}"
    return f"{value:.6g}"


_DEFAULT_LEVERAGE_FALLBACK = 5


def _effective_leverage(
    signal: OpenSignal, settings_last_used: int | None,
) -> int:
    """Decide the leverage to prefill on the card.

    Priority: (1) leverage on the signal itself (structured perp bot
    calls always have it); (2) the user's last-used leverage from
    user_trading_settings; (3) the hard fallback of 5x.
    """
    if signal.leverage and signal.leverage > 0:
        return int(signal.leverage)
    if settings_last_used and settings_last_used > 0:
        return int(settings_last_used)
    return _DEFAULT_LEVERAGE_FALLBACK


_CONDITIONAL_SL_WARNING = (
    "<i>Note: SL fires on price touch, not candle close. "
    "Adjust on Ostium after if you want a candle-close SL.</i>"
)


def _format_card(
    signal: OpenSignal,
    *,
    slippage_bps: int,
    presets: list[float],
    last_used_size: float | None,
    effective_leverage: int,
) -> tuple[str, InlineKeyboardMarkup]:
    side_label = signal.side or "?"
    lines = [
        f"<b>1-Tap Trade — {signal.pair}</b>",
        "",
        f"Direction: <b>{side_label}</b>   Leverage: <b>{effective_leverage}x</b>",
        f"Entry: <code>{_fmt_price(signal.entry)}</code>",
        f"TP1: <code>{_fmt_price(signal.tp1)}</code>"
        + (f"   TP2: <code>{_fmt_price(signal.tp2)}</code>" if signal.tp2 else "")
        + (f"   TP3: <code>{_fmt_price(signal.tp3)}</code>" if signal.tp3 else ""),
        f"Stop: <code>{_fmt_price(signal.stop_loss)}</code>",
    ]
    if signal.stop_loss_is_conditional:
        lines.append(_CONDITIONAL_SL_WARNING)
    lines += [
        "",
        f"Slippage: {slippage_bps / 100:.2f}%",
        "",
        "<b>Choose size (USDC):</b>",
    ]
    if last_used_size is not None:
        lines.append(f"<i>Last used: ${last_used_size:g}</i>")

    size_buttons = [
        InlineKeyboardButton(
            text=f"${int(p)}",
            callback_data=f"{_CB_SIZE_PREFIX}{p:g}",
        )
        for p in presets
    ]
    # Wrap presets into rows of up to 4 buttons.
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(size_buttons), 4):
        rows.append(size_buttons[i : i + 4])
    rows.append([
        InlineKeyboardButton("Custom", callback_data=_CB_CUSTOM),
        InlineKeyboardButton("Cancel", callback_data=_CB_CANCEL),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _format_confirm(
    signal: OpenSignal,
    size: float,
    slippage_bps: int,
    effective_leverage: int,
) -> tuple[str, InlineKeyboardMarkup]:
    side_label = signal.side or "?"
    sl_line = (
        f"Entry: <code>{_fmt_price(signal.entry)}</code>   "
        f"Stop: <code>{_fmt_price(signal.stop_loss)}</code>"
    )
    text_parts = [
        "<b>Confirm Trade</b>",
        "",
        f"{signal.pair}  <b>{side_label} {effective_leverage}x</b>",
        f"Size: <b>${size:g}</b> USDC",
        sl_line,
    ]
    if signal.stop_loss_is_conditional:
        text_parts.append(_CONDITIONAL_SL_WARNING)
    text_parts += [
        f"TP1: <code>{_fmt_price(signal.tp1)}</code>",
        f"Slippage: {slippage_bps / 100:.2f}%",
        "",
        "This will fire immediately on Ostium.",
    ]
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Confirm", callback_data=_CB_CONFIRM),
            InlineKeyboardButton("Cancel", callback_data=_CB_CANCEL),
        ],
    ])
    return "\n".join(text_parts), keyboard


class TradeFlow:
    """Wires the 1-Tap Trade callback handlers and the custom-size
    text-reply handler.

    Construct once per bot, register on the Telegram Application.
    """

    def __init__(
        self,
        config: Config,
        verification_db: VerificationDB,
        delegates_db: DelegatesDB,
        settings_db: UserTradingSettingsDB,
        open_signals_db: OpenSignalsDB,
        executor: TradeExecutorClient,
    ):
        self._config = config
        self._verification_db = verification_db
        self._delegates_db = delegates_db
        self._settings_db = settings_db
        self._open_signals_db = open_signals_db
        self._executor = executor

    def register(self, application: Application) -> None:
        application.add_handler(
            CallbackQueryHandler(self._cb_open, pattern=f"^{_CB_OPEN_PREFIX}")
        )
        application.add_handler(
            CallbackQueryHandler(self._cb_size, pattern=f"^{_CB_SIZE_PREFIX}")
        )
        application.add_handler(
            CallbackQueryHandler(self._cb_custom, pattern=f"^{_CB_CUSTOM}$")
        )
        application.add_handler(
            CallbackQueryHandler(self._cb_confirm, pattern=f"^{_CB_CONFIRM}$")
        )
        application.add_handler(
            CallbackQueryHandler(self._cb_cancel, pattern=f"^{_CB_CANCEL}$")
        )
        # Custom-size text reply. Same group=99 as the /connect paste handler
        # so it does not capture commands or menu taps.
        application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._on_text_while_awaiting_size,
            ),
            group=99,
        )

    # ---- shared helpers ----------------------------------------------------

    def _get_state(self, ctx: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
        if ctx.user_data is None:
            return {}
        state = ctx.user_data.get(_USER_DATA_KEY)
        if not isinstance(state, dict):
            return {}
        return state

    def _set_state(
        self, ctx: ContextTypes.DEFAULT_TYPE, state: dict[str, Any] | None,
    ) -> None:
        if ctx.user_data is None:
            return
        if state is None:
            ctx.user_data.pop(_USER_DATA_KEY, None)
        else:
            ctx.user_data[_USER_DATA_KEY] = state

    async def _signal_or_none(
        self, signal_id: int,
    ) -> OpenSignal | None:
        try:
            return await self._open_signals_db.find_by_id(signal_id)
        except Exception:
            logger.exception("find_by_id failed for signal %d", signal_id)
            return None

    # ---- qt:open ----------------------------------------------------------

    async def _cb_open(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        if not query or not query.data or not update.effective_user:
            return
        user_id = update.effective_user.id

        try:
            signal_id = int(query.data.removeprefix(_CB_OPEN_PREFIX))
        except ValueError:
            await query.answer("Invalid signal", show_alert=False)
            return

        if not self._config.trading.enabled:
            await query.answer(
                "1-Tap Trade is not enabled on this bot yet.",
                show_alert=True,
            )
            return

        verified = await self._verification_db.get_verified(user_id)
        if verified is None or not verified.is_active:
            await query.answer(
                "1-Tap Trade is Elite-only. Run /verify first.",
                show_alert=True,
            )
            return

        delegate = await self._delegates_db.get(user_id)
        if delegate is None or not delegate.is_active:
            await query.answer()
            await ctx.bot.send_message(
                chat_id=user_id,
                text=(
                    "<b>One-time setup needed</b>\n\n"
                    "Run /connect first to enable 1-Tap Trade. You only "
                    "need to do this once."
                ),
                parse_mode="HTML",
            )
            return

        signal = await self._signal_or_none(signal_id)
        if signal is None:
            await query.answer(
                "That signal is no longer available.", show_alert=True,
            )
            return

        import time
        if int(time.time()) - signal.opened_at > _MAX_SIGNAL_AGE_SEC:
            await query.answer(
                "Signal is older than 24h — price has likely moved.",
                show_alert=True,
            )
            return

        settings = await self._settings_db.get_or_default(user_id)
        effective_leverage = _effective_leverage(
            signal, settings.last_used_leverage,
        )
        text, keyboard = _format_card(
            signal,
            slippage_bps=settings.slippage_bps,
            presets=settings.size_presets,
            last_used_size=settings.last_used_size,
            effective_leverage=effective_leverage,
        )
        self._set_state(ctx, {
            "signal_id": signal_id,
            "size": None,
            "awaiting_size": False,
            "effective_leverage": effective_leverage,
        })
        await query.answer()
        await ctx.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    # ---- qt:size ----------------------------------------------------------

    async def _cb_size(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        try:
            size = float(query.data.removeprefix(_CB_SIZE_PREFIX))
        except ValueError:
            await query.answer("Invalid size", show_alert=False)
            return
        await self._present_confirm(update, ctx, size)

    # ---- qt:custom --------------------------------------------------------

    async def _cb_custom(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        if not query:
            return
        state = self._get_state(ctx)
        if not state or "signal_id" not in state:
            await query.answer("Nothing pending", show_alert=False)
            return
        state["awaiting_size"] = True
        self._set_state(ctx, state)
        await query.answer()
        await query.edit_message_text(
            "<b>Custom size</b>\n\n"
            f"How much USDC do you want to trade? Max "
            f"${self._config.trading.max_collateral_usdc:g}.\n\n"
            "Reply with a number, e.g. <code>75</code>. Send /cancel to abort.",
            parse_mode="HTML",
        )

    # ---- plain text while awaiting size -----------------------------------

    async def _on_text_while_awaiting_size(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not update.effective_message or not update.effective_user:
            return
        state = self._get_state(ctx)
        if not state.get("awaiting_size"):
            return
        text = (update.effective_message.text or "").strip()
        if text.lower() in ("/cancel", "cancel"):
            self._set_state(ctx, None)
            await update.effective_message.reply_text("Cancelled.")
            return
        try:
            size = float(text.replace(",", "").replace("$", ""))
        except ValueError:
            await update.effective_message.reply_text(
                "Could not read that as a number. Try something like "
                "<code>75</code>, or send /cancel.",
                parse_mode="HTML",
            )
            return
        max_size = self._config.trading.max_collateral_usdc
        if size <= 0 or size > max_size:
            await update.effective_message.reply_text(
                f"Size must be between $0 and ${max_size:g}."
            )
            return
        state["awaiting_size"] = False
        self._set_state(ctx, state)
        await self._present_confirm(update, ctx, size)

    # ---- shared confirm-card builder --------------------------------------

    async def _present_confirm(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        size: float,
    ) -> None:
        if not update.effective_user:
            return
        user_id = update.effective_user.id
        state = self._get_state(ctx)
        signal_id = state.get("signal_id") if state else None
        if not isinstance(signal_id, int):
            if update.callback_query:
                await update.callback_query.answer("Nothing pending", show_alert=False)
            return
        signal = await self._signal_or_none(signal_id)
        if signal is None:
            if update.callback_query:
                await update.callback_query.answer(
                    "Signal expired.", show_alert=True,
                )
            return

        settings = await self._settings_db.get_or_default(user_id)
        effective_leverage = state.get("effective_leverage") if state else None
        if not isinstance(effective_leverage, int) or effective_leverage <= 0:
            effective_leverage = _effective_leverage(
                signal, settings.last_used_leverage,
            )
        text, keyboard = _format_confirm(
            signal, size, settings.slippage_bps, effective_leverage,
        )

        state["size"] = size
        state["awaiting_size"] = False
        state["effective_leverage"] = effective_leverage
        self._set_state(ctx, state)

        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                text, parse_mode="HTML", reply_markup=keyboard,
            )
        else:
            await ctx.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )

    # ---- qt:confirm -------------------------------------------------------

    async def _cb_confirm(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        if not query or not update.effective_user:
            return
        user_id = update.effective_user.id
        state = self._get_state(ctx)
        signal_id = state.get("signal_id") if state else None
        size = state.get("size") if state else None
        if not isinstance(signal_id, int) or not isinstance(size, (int, float)):
            await query.answer("Nothing pending", show_alert=False)
            return

        signal = await self._signal_or_none(signal_id)
        if signal is None:
            await query.answer("Signal expired.", show_alert=True)
            self._set_state(ctx, None)
            return

        delegate = await self._delegates_db.get(user_id)
        if delegate is None or not delegate.is_active:
            await query.answer(
                "Not connected. Run /connect first.", show_alert=True,
            )
            self._set_state(ctx, None)
            return

        settings = await self._settings_db.get_or_default(user_id)
        plaintext_key = await self._delegates_db.get_plaintext_key(user_id)
        if plaintext_key is None:
            await query.answer("Delegate key missing.", show_alert=True)
            self._set_state(ctx, None)
            return

        pair_base = (
            signal.normalised_base or (signal.pair.split("/")[0] if signal.pair else "")
        ).upper()

        effective_leverage = state.get("effective_leverage") if state else None
        if not isinstance(effective_leverage, int) or effective_leverage <= 0:
            effective_leverage = _effective_leverage(
                signal, settings.last_used_leverage,
            )

        await query.answer()
        await query.edit_message_text(
            "Submitting trade...", parse_mode="HTML",
        )

        try:
            result = await self._executor.submit_trade(
                trader_address=delegate.trader_address,
                delegate_private_key=plaintext_key,
                builder_address=self._config.trading.builder_address or None,
                builder_fee_bps=self._config.trading.builder_fee_bps,
                pair_base=pair_base,
                direction=signal.side or "LONG",
                leverage=int(effective_leverage),
                collateral_usdc=float(size),
                slippage_bps=settings.slippage_bps,
                take_profit=signal.tp1,
                stop_loss=signal.stop_loss,
                order_type="MARKET",
            )
        except ExecutorError as e:
            await self._delegates_db.mark_trade_failure(user_id, str(e))
            self._set_state(ctx, None)
            await query.edit_message_text(
                f"<b>Trade failed</b>\n\n<i>{str(e)}</i>\n\n"
                "Nothing was opened on Ostium. Try again or check "
                "your USDC balance/allowance.",
                parse_mode="HTML",
            )
            logger.info(
                "Trade failed for user=%d signal=%d size=%s: %s",
                user_id, signal_id, size, e,
            )
            return
        except Exception as e:
            await self._delegates_db.mark_trade_failure(user_id, repr(e))
            self._set_state(ctx, None)
            await query.edit_message_text(
                "<b>Trade failed</b>\n\nUnexpected error. Nothing was "
                "opened on Ostium.",
                parse_mode="HTML",
            )
            logger.exception(
                "Unexpected trade failure for user=%d signal=%d",
                user_id, signal_id,
            )
            return

        await self._delegates_db.mark_trade_success(user_id)
        await self._settings_db.mark_size_used(user_id, float(size))
        try:
            await self._settings_db.set_last_used_leverage(
                user_id, int(effective_leverage),
            )
        except Exception:
            logger.exception(
                "set_last_used_leverage failed for user=%d", user_id,
            )
        self._set_state(ctx, None)
        await query.edit_message_text(
            f"<b>Trade submitted ✅</b>\n\n"
            f"{signal.pair} {signal.side} {effective_leverage}x  "
            f"size <b>${size:g}</b>\n"
            f"Tx: <a href=\"https://arbiscan.io/tx/{result.tx_hash}\">"
            f"{result.tx_hash[:10]}...{result.tx_hash[-6:]}</a>\n\n"
            f"Manage the position on "
            f"<a href=\"https://app.ostium.com\">app.ostium.com</a>.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info(
            "Trade submitted user=%d signal=%d size=%s tx=%s",
            user_id, signal_id, size, result.tx_hash,
        )

    # ---- qt:cancel --------------------------------------------------------

    async def _cb_cancel(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        if not query:
            return
        self._set_state(ctx, None)
        await query.answer("Cancelled.")
        await query.edit_message_text("Trade cancelled.")
