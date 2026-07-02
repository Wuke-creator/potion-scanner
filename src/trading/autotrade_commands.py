"""Telegram commands for Hyperliquid autotrade opt-in (allowlist-gated).

Self-contained so it doesn't entangle the Ostium 1-Tap flow. Reuses the
shared DelegatesDB (master address + agent key) and AutotradePrefsDB.

  /autotrade                 status + help
  /autotrade connect         disclosure + how-to, then await a pasted
                             address/key (stored in DelegatesDB)
  /autotrade on              show the auto-execution disclosure to accept
  /autotrade agree           accept disclosure + enable (needs connected)
  /autotrade off             disable (stops firing immediately)
  /autotrade size <pct>      set percent-of-balance per trade
  /autotrade disconnect      wipe the stored key + disable

Only Telegram IDs in AUTOTRADE_ALLOWLIST can use it, and only Elite-verified
users can connect. Connecting/opting-in is allowed while the feature is in
dry-run; the engine simply previews until AUTOTRADE_DRY_RUN is turned off.
"""

from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.config import Config
from src.trading.autotrade_prefs_db import AutotradePrefsDB, MAX_SIZE_PCT, MIN_SIZE_PCT
from src.trading.delegates_db import DelegatesDB
from src.verification.db import VerificationDB

logger = logging.getLogger(__name__)

_STATE_KEY = "autotrade_state"
_STATE_AWAITING = "awaiting_connect_paste"

_ADDR_RE = re.compile(
    r"(?:address|master|account)\s*[:=]\s*(0x[a-fA-F0-9]{40})", re.IGNORECASE
)
_KEY_RE = re.compile(
    r"(?:key|agent|delegate)\s*[:=]\s*(0x[a-fA-F0-9]{64})", re.IGNORECASE
)

_CONNECT_HOWTO = (
    "<b>Connect your Hyperliquid agent wallet</b>\n\n"
    "1. Go to app.hyperliquid.xyz, connect your wallet.\n"
    "2. Open <b>More -> API</b>.\n"
    "3. <b>Generate</b> an agent wallet and copy its private key. "
    "Then <b>Authorize</b> it (sign in your wallet). This key can place "
    "orders but <b>cannot withdraw</b> your funds.\n"
    "4. Reply here with two lines:\n\n"
    "<code>address: 0xYourMainAccountAddress</code>\n"
    "<code>key: 0xYourAgentPrivateKey</code>\n\n"
    "I store the key encrypted and delete your message. Send /cancel to abort."
)

_AUTOEXEC_DISCLOSURE = (
    "<b>Autotrade - read before enabling</b>\n\n"
    "When you enable this, the bot will <b>automatically open real trades</b> "
    "on your Hyperliquid account whenever a Perp Bot Calls signal fires. "
    "Each trade uses a percent of your available USDC (default 5%), the "
    "signal's direction and leverage (capped), and the signal's TP1 and stop. "
    "You can lose money, including on bad or fast-moving signals.\n\n"
    "You stay in control: <code>/autotrade off</code> stops it immediately, "
    "and you manage or close any position yourself on app.hyperliquid.xyz.\n\n"
    "If you understand and accept this, run <code>/autotrade agree</code>."
)


class AutotradeCommands:
    def __init__(
        self,
        config: Config,
        verification_db: VerificationDB,
        delegates_db: DelegatesDB,
        prefs_db: AutotradePrefsDB,
    ):
        self._config = config
        self._verification_db = verification_db
        self._delegates_db = delegates_db
        self._prefs_db = prefs_db

    def register(self, application: Application) -> None:
        application.add_handler(CommandHandler("autotrade", self._cmd))
        application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._on_text_while_awaiting,
            ),
            group=98,
        )

    # ---- helpers -----------------------------------------------------------

    def _allowlisted(self, uid: int) -> bool:
        return uid in self._config.autotrade.allowlist

    async def _is_elite(self, uid: int) -> bool:
        rec = await self._verification_db.get_verified(uid)
        return rec is not None and rec.is_active

    async def _is_connected(self, uid: int) -> bool:
        d = await self._delegates_db.get(uid)
        return d is not None and d.is_active

    # ---- command dispatch --------------------------------------------------

    async def _cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not update.effective_message:
            return
        uid = update.effective_user.id
        reply = update.effective_message.reply_text

        if not self._allowlisted(uid):
            await reply("Autotrade is not enabled for your account.")
            return

        args = [a.lower() for a in (ctx.args or [])]
        sub = args[0] if args else ""

        if sub in ("", "status"):
            await reply(await self._status_text(uid), parse_mode="HTML")
        elif sub == "connect":
            if not await self._is_elite(uid):
                await reply("Autotrade is Elite-only. Run /verify first.")
                return
            if ctx.user_data is not None:
                ctx.user_data[_STATE_KEY] = _STATE_AWAITING
            await reply(
                _CONNECT_HOWTO, parse_mode="HTML", disable_web_page_preview=True,
            )
        elif sub in ("on", "enable"):
            if not await self._is_connected(uid):
                await reply(
                    "Connect your Hyperliquid wallet first: /autotrade connect"
                )
                return
            await reply(
                _AUTOEXEC_DISCLOSURE, parse_mode="HTML",
                disable_web_page_preview=True,
            )
        elif sub == "agree":
            if not await self._is_connected(uid):
                await reply("Connect first: /autotrade connect")
                return
            await self._prefs_db.accept_disclosure(uid)
            await self._prefs_db.set_enabled(uid, True)
            await reply(
                "Autotrade <b>enabled</b>. It will act on new Perp Bot signals.\n"
                "Use <code>/autotrade off</code> to stop, "
                "<code>/autotrade size &lt;pct&gt;</code> to change size.",
                parse_mode="HTML",
            )
            logger.info("autotrade enabled for user=%d", uid)
        elif sub in ("off", "disable"):
            await self._prefs_db.set_enabled(uid, False)
            await reply("Autotrade <b>disabled</b>. No further trades will fire.", parse_mode="HTML")
            logger.info("autotrade disabled for user=%d", uid)
        elif sub == "size":
            await self._set_size(uid, args, reply)
        elif sub == "disconnect":
            await self._delegates_db.delete(uid)
            await self._prefs_db.set_enabled(uid, False)
            await reply("Disconnected and disabled. Your stored key was wiped.")
            logger.info("autotrade disconnected for user=%d", uid)
        else:
            await reply(
                "Autotrade commands:\n"
                "/autotrade - status\n"
                "/autotrade connect - link your Hyperliquid wallet\n"
                "/autotrade on - enable (shows disclosure)\n"
                "/autotrade agree - accept + enable\n"
                "/autotrade size <pct> - set percent of balance per trade\n"
                "/autotrade off - stop\n"
                "/autotrade disconnect - wipe key + stop"
            )

    async def _set_size(self, uid: int, args: list[str], reply) -> None:
        if len(args) < 2:
            await reply("Usage: /autotrade size <percent>, e.g. /autotrade size 5")
            return
        try:
            pct = float(args[1].rstrip("%"))
        except ValueError:
            await reply("That is not a number. Example: /autotrade size 5")
            return
        try:
            await self._prefs_db.set_size_pct(uid, pct)
        except ValueError:
            await reply(
                f"Size must be between {MIN_SIZE_PCT} and {MAX_SIZE_PCT} percent."
            )
            return
        await reply(f"Autotrade size set to {pct:g}% of available balance per trade.")

    async def _status_text(self, uid: int) -> str:
        connected = await self._is_connected(uid)
        prefs = await self._prefs_db.get_or_default(
            uid, default_pct=self._config.autotrade.default_size_pct,
        )
        at = self._config.autotrade
        mode = "DRY-RUN (previews only)" if at.dry_run else "LIVE"
        lines = [
            "<b>Autotrade status</b>",
            f"Global: {'on' if at.enabled else 'off'} - {mode} - {at.network}",
            f"Connected: {'yes' if connected else 'no'}",
            f"Your switch: {'ON' if prefs.enabled else 'off'}"
            + ("" if prefs.disclosure_accepted_at else " (disclosure not accepted)"),
            f"Size: {prefs.size_pct:g}% of balance per trade",
            f"Daily cap: {at.max_per_day} trades, max leverage {at.max_leverage}x",
        ]
        if not connected:
            lines.append("\nStart with /autotrade connect")
        elif not prefs.ready:
            lines.append("\nEnable with /autotrade on")
        return "\n".join(lines)

    # ---- paste handler -----------------------------------------------------

    async def _on_text_while_awaiting(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not update.effective_user or not update.effective_message:
            return
        if ctx.user_data is None or ctx.user_data.get(_STATE_KEY) != _STATE_AWAITING:
            return
        uid = update.effective_user.id
        if not self._allowlisted(uid):
            return
        text = update.effective_message.text or ""
        if text.strip().lower() in ("/cancel", "cancel"):
            ctx.user_data.pop(_STATE_KEY, None)
            await update.effective_message.reply_text("Cancelled.")
            return

        addr_m = _ADDR_RE.search(text)
        key_m = _KEY_RE.search(text)
        if not addr_m or not key_m:
            await update.effective_message.reply_text(
                "I need both lines:\n"
                "<code>address: 0x...(40 hex)</code>\n"
                "<code>key: 0x...(64 hex)</code>\n"
                "Try again, or send /cancel.",
                parse_mode="HTML",
            )
            return

        address, agent_key = addr_m.group(1), key_m.group(1)
        try:
            await self._delegates_db.upsert(
                telegram_user_id=uid,
                trader_address=address,
                delegate_private_key=agent_key,
            )
        except Exception:
            logger.exception("autotrade connect store failed for user=%d", uid)
            await update.effective_message.reply_text(
                "Something went wrong storing your key. Nothing was saved. "
                "Try /autotrade connect again."
            )
            return

        ctx.user_data.pop(_STATE_KEY, None)
        try:
            await update.effective_message.delete()
        except Exception:
            logger.debug("could not delete connect paste for user=%d", uid)

        await ctx.bot.send_message(
            chat_id=uid,
            text=(
                "<b>Connected.</b>\n"
                f"Account: <code>{address}</code>\n\n"
                "Now enable it: <code>/autotrade on</code>, then "
                "<code>/autotrade agree</code>."
            ),
            parse_mode="HTML",
        )
        logger.info("autotrade connected for user=%d account=%s", uid, address)
