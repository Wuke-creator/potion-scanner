"""Trading settings UI: slippage + size presets, per-user.

Surfaces:

  /trading-settings   Open the panel. Shows current slippage and presets
                      with inline buttons to edit either.

Callbacks:

  ts:open             Re-render the panel (used after edits).
  ts:slip:{bps}       Set slippage to a quick value (10/25/50/100/200 bps).
  ts:slip_custom      Switch to "awaiting custom slippage" state.
  ts:presets_edit     Switch to "awaiting presets edit" state. The user
                      replies with a comma-separated USDC list, e.g.
                      "25, 50, 100, 250".
  ts:cancel           Wipe pending state.

State in ctx.user_data["trading_settings_state"]:

  "awaiting_slippage" | "awaiting_presets" | None
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.trading.user_settings_db import (
    MAX_PRESETS,
    MAX_SLIPPAGE_BPS,
    MIN_SLIPPAGE_BPS,
    UserTradingSettings,
    UserTradingSettingsDB,
)

logger = logging.getLogger(__name__)


_STATE_KEY = "trading_settings_state"
_S_AWAITING_SLIP = "awaiting_slippage"
_S_AWAITING_PRESETS = "awaiting_presets"

_CB_OPEN = "ts:open"
_CB_SLIP_PREFIX = "ts:slip:"
_CB_SLIP_CUSTOM = "ts:slip_custom"
_CB_PRESETS_EDIT = "ts:presets_edit"
_CB_CANCEL = "ts:cancel"

_QUICK_SLIPPAGE_BPS = [10, 25, 50, 100, 200]


def _render_panel(s: UserTradingSettings) -> tuple[str, InlineKeyboardMarkup]:
    presets_text = ", ".join(f"${int(p)}" if p == int(p) else f"${p:g}" for p in s.size_presets)
    text = (
        "<b>Trading Preferences</b>\n\n"
        f"Slippage tolerance: <b>{s.slippage_bps / 100:.2f}%</b>\n"
        f"Size presets: <b>{presets_text}</b>\n\n"
        "Pick a slippage below, or tap <b>Edit presets</b> to change "
        "your quick-size buttons."
    )

    slip_buttons = [
        InlineKeyboardButton(
            text=("• " if s.slippage_bps == bps else "")
            + f"{bps / 100:.2f}%",
            callback_data=f"{_CB_SLIP_PREFIX}{bps}",
        )
        for bps in _QUICK_SLIPPAGE_BPS
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(slip_buttons), 3):
        rows.append(slip_buttons[i : i + 3])
    rows.append([InlineKeyboardButton("Custom slippage", callback_data=_CB_SLIP_CUSTOM)])
    rows.append([InlineKeyboardButton("Edit presets", callback_data=_CB_PRESETS_EDIT)])
    return text, InlineKeyboardMarkup(rows)


def _parse_presets(text: str) -> list[float]:
    raw = text.replace("$", "").replace(";", ",").split(",")
    out: list[float] = []
    for part in raw:
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    return out


class TradingSettingsUI:
    """Wires /trading-settings + its inline-keyboard sub-flows."""

    def __init__(self, settings_db: UserTradingSettingsDB):
        self._settings_db = settings_db

    def register(self, application: Application) -> None:
        application.add_handler(
            CommandHandler("trading_settings", self._cmd_open)
        )
        application.add_handler(
            CommandHandler("tradingsettings", self._cmd_open)
        )
        application.add_handler(
            CallbackQueryHandler(self._cb_open, pattern=f"^{_CB_OPEN}$")
        )
        application.add_handler(
            CallbackQueryHandler(self._cb_slip, pattern=f"^{_CB_SLIP_PREFIX}")
        )
        application.add_handler(
            CallbackQueryHandler(self._cb_slip_custom, pattern=f"^{_CB_SLIP_CUSTOM}$")
        )
        application.add_handler(
            CallbackQueryHandler(self._cb_presets_edit, pattern=f"^{_CB_PRESETS_EDIT}$")
        )
        application.add_handler(
            CallbackQueryHandler(self._cb_cancel, pattern=f"^{_CB_CANCEL}$")
        )
        application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._on_text_while_awaiting,
            ),
            group=99,
        )

    def _state(self, ctx: ContextTypes.DEFAULT_TYPE) -> Any:
        if ctx.user_data is None:
            return None
        return ctx.user_data.get(_STATE_KEY)

    def _set_state(self, ctx: ContextTypes.DEFAULT_TYPE, value: Any) -> None:
        if ctx.user_data is None:
            return
        if value is None:
            ctx.user_data.pop(_STATE_KEY, None)
        else:
            ctx.user_data[_STATE_KEY] = value

    async def _cmd_open(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not update.effective_user or not update.effective_message:
            return
        s = await self._settings_db.get_or_default(update.effective_user.id)
        text, keyboard = _render_panel(s)
        await update.effective_message.reply_text(
            text, parse_mode="HTML", reply_markup=keyboard,
        )

    async def _cb_open(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not update.callback_query or not update.effective_user:
            return
        s = await self._settings_db.get_or_default(update.effective_user.id)
        text, keyboard = _render_panel(s)
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text, parse_mode="HTML", reply_markup=keyboard,
        )

    async def _cb_slip(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        if not query or not query.data or not update.effective_user:
            return
        try:
            bps = int(query.data.removeprefix(_CB_SLIP_PREFIX))
        except ValueError:
            await query.answer("Invalid", show_alert=False)
            return
        try:
            await self._settings_db.set_slippage(
                update.effective_user.id, bps,
            )
        except ValueError as e:
            await query.answer(str(e), show_alert=True)
            return
        s = await self._settings_db.get_or_default(update.effective_user.id)
        text, keyboard = _render_panel(s)
        await query.answer(f"Slippage set to {bps / 100:.2f}%")
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=keyboard,
        )

    async def _cb_slip_custom(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        if not query:
            return
        self._set_state(ctx, _S_AWAITING_SLIP)
        await query.answer()
        await query.edit_message_text(
            "<b>Custom slippage</b>\n\n"
            "Reply with a percentage, e.g. <code>0.35</code> for 0.35%. "
            f"Range: {MIN_SLIPPAGE_BPS / 100:.2f}% to {MAX_SLIPPAGE_BPS / 100:.2f}%.\n\n"
            "Send /cancel to abort.",
            parse_mode="HTML",
        )

    async def _cb_presets_edit(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        if not query:
            return
        self._set_state(ctx, _S_AWAITING_PRESETS)
        await query.answer()
        await query.edit_message_text(
            "<b>Edit size presets</b>\n\n"
            "Reply with up to "
            f"{MAX_PRESETS} USDC amounts, comma-separated.\n"
            "Example: <code>25, 50, 100, 250</code>\n\n"
            "Send /cancel to abort.",
            parse_mode="HTML",
        )

    async def _cb_cancel(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        if not query:
            return
        self._set_state(ctx, None)
        await query.answer("Cancelled.")
        await query.edit_message_text("Cancelled.")

    async def _on_text_while_awaiting(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not update.effective_user or not update.effective_message:
            return
        state = self._state(ctx)
        if state not in (_S_AWAITING_SLIP, _S_AWAITING_PRESETS):
            return
        user_id = update.effective_user.id
        text = (update.effective_message.text or "").strip()

        if text.lower() in ("/cancel", "cancel"):
            self._set_state(ctx, None)
            await update.effective_message.reply_text("Cancelled.")
            return

        if state == _S_AWAITING_SLIP:
            try:
                percent = float(text.replace("%", "").strip())
                bps = int(round(percent * 100))
            except ValueError:
                await update.effective_message.reply_text(
                    "Could not read that as a percentage. "
                    "Try <code>0.5</code> or send /cancel.",
                    parse_mode="HTML",
                )
                return
            try:
                await self._settings_db.set_slippage(user_id, bps)
            except ValueError as e:
                await update.effective_message.reply_text(str(e))
                return
            self._set_state(ctx, None)
            s = await self._settings_db.get_or_default(user_id)
            panel_text, keyboard = _render_panel(s)
            await update.effective_message.reply_text(
                panel_text, parse_mode="HTML", reply_markup=keyboard,
            )
            return

        if state == _S_AWAITING_PRESETS:
            try:
                presets = _parse_presets(text)
            except ValueError:
                await update.effective_message.reply_text(
                    "Could not parse the sizes. Use a comma-separated "
                    "list like <code>25, 50, 100, 250</code>.",
                    parse_mode="HTML",
                )
                return
            try:
                await self._settings_db.set_presets(user_id, presets)
            except ValueError as e:
                await update.effective_message.reply_text(str(e))
                return
            self._set_state(ctx, None)
            s = await self._settings_db.get_or_default(user_id)
            panel_text, keyboard = _render_panel(s)
            await update.effective_message.reply_text(
                panel_text, parse_mode="HTML", reply_markup=keyboard,
            )
            return
