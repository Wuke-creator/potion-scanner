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
_FIRE_PENDING = "autotrade_fire_pending"
_TPS_PENDING = "autotrade_tps_pending"
_FIRE_TTL_SEC = 120

_ADDR_RE = re.compile(
    r"(?:address|master|account)\s*[:=]\s*(0x[a-fA-F0-9]{40})", re.IGNORECASE
)
_KEY_RE = re.compile(
    r"(?:key|agent|delegate)\s*[:=]\s*(0x[a-fA-F0-9]{64})", re.IGNORECASE
)

# Blofin creds paste: three labelled lines. Values are opaque tokens (no
# spaces), so \S+ is the right matcher. Order the patterns so "secret" and
# "passphrase" win over the bare "key" alternative.
_BLOFIN_SECRET_RE = re.compile(r"secret\s*(?:key)?\s*[:=]\s*(\S+)", re.IGNORECASE)
_BLOFIN_PASS_RE = re.compile(r"(?:passphrase|pass\s*phrase|phrase)\s*[:=]\s*(\S+)", re.IGNORECASE)
_BLOFIN_KEY_RE = re.compile(r"(?:api[-_ ]?key|\bkey)\s*[:=]\s*(\S+)", re.IGNORECASE)

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

_BLOFIN_CONNECT_HOWTO = (
    "<b>Connect your Blofin API key</b>\n\n"
    "1. On blofin.com open <b>Account -> APIs -> Create API Key</b>.\n"
    "2. Permissions: <b>Read + Trade</b>. Do <b>NOT</b> enable Withdraw or "
    "Transfer. A trade-only key can place orders but can never move your "
    "funds off the account.\n"
    "3. Set a passphrase you choose, and copy the API key + secret.\n"
    "4. Reply here with three lines:\n\n"
    "<code>key: yourApiKey</code>\n"
    "<code>secret: yourApiSecret</code>\n"
    "<code>passphrase: yourPassphrase</code>\n\n"
    "I store all three encrypted and delete your message. Send /cancel to abort."
)


def _autoexec_disclosure(venue_name: str, manage_url: str) -> str:
    label = "Blofin" if venue_name == "blofin" else "Hyperliquid"
    return (
        "<b>Autotrade - read before enabling</b>\n\n"
        "When you enable this, the bot will <b>automatically open real trades</b> "
        f"on your {label} account whenever a Perp Bot Calls signal fires. "
        "Each trade uses a percent of your available balance (default 5%), the "
        "signal's direction and leverage (capped), and the signal's TP1 and stop. "
        "You can lose money, including on bad or fast-moving signals.\n\n"
        "You stay in control: <code>/autotrade off</code> stops it immediately, "
        f"and you manage or close any position yourself on {manage_url}.\n\n"
        "If you understand and accept this, run <code>/autotrade agree</code>."
    )


class AutotradeCommands:
    def __init__(
        self,
        config: Config,
        verification_db: VerificationDB,
        prefs_db: AutotradePrefsDB,
        engine=None,
        venue=None,
        delegates_db: DelegatesDB | None = None,
        blofin_creds_db=None,
        open_signals_db=None,
        signal_channel_id: int = 0,
    ):
        self._config = config
        self._verification_db = verification_db
        self._prefs_db = prefs_db
        self._engine = engine
        self._venue = venue
        self._delegates_db = delegates_db
        self._blofin_creds_db = blofin_creds_db
        self._open_signals_db = open_signals_db
        self._signal_channel_id = signal_channel_id

    def _venue_name(self) -> str:
        return getattr(self._config.autotrade, "venue", "hyperliquid")

    def _manage_url(self) -> str:
        return "blofin.com" if self._venue_name() == "blofin" else "app.hyperliquid.xyz"

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
        if self._venue is not None:
            conn = await self._venue.get_connection(uid)
            return conn is not None and conn.is_active
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
            howto = (
                _BLOFIN_CONNECT_HOWTO
                if self._venue_name() == "blofin"
                else _CONNECT_HOWTO
            )
            await reply(
                howto, parse_mode="HTML", disable_web_page_preview=True,
            )
        elif sub in ("on", "enable"):
            if not await self._is_connected(uid):
                await reply(
                    "Connect first: /autotrade connect"
                )
                return
            await reply(
                _autoexec_disclosure(self._venue_name(), self._manage_url()),
                parse_mode="HTML",
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
        elif sub == "fire":
            await self._fire(uid, args, ctx, reply)
        elif sub == "tps":
            await self._tps(uid, args, ctx, reply)
        elif sub == "size":
            await self._set_size(uid, args, reply)
        elif sub == "disconnect":
            if self._venue_name() == "blofin" and self._blofin_creds_db is not None:
                await self._blofin_creds_db.delete(uid)
            elif self._delegates_db is not None:
                await self._delegates_db.delete(uid)
            await self._prefs_db.set_enabled(uid, False)
            await reply("Disconnected and disabled. Your stored credentials were wiped.")
            logger.info("autotrade disconnected for user=%d", uid)
        else:
            await reply(
                "Autotrade commands:\n"
                "/autotrade - status\n"
                "/autotrade connect - link your Hyperliquid wallet\n"
                "/autotrade on - enable (shows disclosure)\n"
                "/autotrade agree - accept + enable\n"
                "/autotrade size <pct> - set percent of balance per trade\n"
                "/autotrade fire <coin> <long|short> [lev] [sl] - manually place one trade\n"
                "/autotrade tps <coin> [px1 px2 px3] - ladder TPs onto an open position\n"
                "/autotrade off - stop\n"
                "/autotrade disconnect - wipe key + stop"
            )

    async def _fire(self, uid, args, ctx, reply) -> None:
        """Manual single trade the user pulls the trigger on.

        Two steps for safety: `/autotrade fire <coin> <long|short> [lev] [sl]`
        previews, then `/autotrade fire confirm` places it. Places a REAL
        order via the same engine path as an auto-fired signal.
        """
        import time

        if self._engine is None:
            await reply("Manual fire is not available on this bot.")
            return
        if not await self._is_connected(uid):
            await reply("Connect first: /autotrade connect")
            return
        prefs = await self._prefs_db.get_or_default(uid)
        if not prefs.ready:
            await reply("Enable autotrade first: /autotrade on then /autotrade agree")
            return

        # --- confirm step ---
        if len(args) >= 2 and args[1] == "confirm":
            pend = (ctx.user_data or {}).get(_FIRE_PENDING)
            if not pend or (time.time() - pend["ts"]) > _FIRE_TTL_SEC:
                await reply("Nothing pending (or it expired). Start with "
                            "/autotrade fire <coin> <long|short> [leverage] [sl].")
                return
            ctx.user_data.pop(_FIRE_PENDING, None)
            await reply(f"Firing {pend['side']} {pend['coin']} now...")
            await self._engine.manual_fire(
                uid, pair=pend["pair"], side=pend["side"],
                leverage=pend["lev"], stop_loss=pend["sl"],
            )
            return

        # --- preview step ---
        if len(args) < 3:
            await reply("Usage: /autotrade fire <coin> <long|short> [leverage] [sl]\n"
                        "e.g. /autotrade fire XLM short 5 0.20748")
            return
        coin = args[1].upper()
        side = args[2].upper()
        if side not in ("LONG", "SHORT"):
            await reply("Direction must be long or short.")
            return
        lev = 0
        if len(args) >= 4:
            try:
                lev = int(args[3])
            except ValueError:
                await reply("Leverage must be a whole number.")
                return
        sl = None
        if len(args) >= 5:
            try:
                sl = float(args[4])
            except ValueError:
                await reply("Stop loss must be a number.")
                return

        pair = f"{coin}/USDT"
        if ctx.user_data is not None:
            ctx.user_data[_FIRE_PENDING] = {
                "pair": pair, "coin": coin, "side": side,
                "lev": lev, "sl": sl, "ts": time.time(),
            }
        await reply(
            f"<b>Confirm manual trade</b>\n"
            f"REAL {side} {coin} at market"
            + (f", {lev}x" if lev else " (leverage capped to the asset max)")
            + (f", SL {sl}" if sl is not None else ", no stop")
            + f"\nSized at your {prefs.size_pct:g}% of balance.\n\n"
            "Reply <code>/autotrade fire confirm</code> within 2 minutes to place it.",
            parse_mode="HTML",
        )

    async def _tps(self, uid, args, ctx, reply) -> None:
        """Lay the signal's TP ladder onto an existing open position.

        `/autotrade tps <coin>` pulls the latest open signal's TP1/2/3 for
        that coin; `/autotrade tps <coin> <px1> [px2] [px3]` uses explicit
        prices. Both preview first; `/autotrade tps confirm` places the
        reduce-only orders on the CURRENT position size.
        """
        import time

        if self._engine is None:
            await reply("TP ladder is not available on this bot.")
            return
        if not await self._is_connected(uid):
            await reply("Connect first: /autotrade connect")
            return

        # --- confirm step ---
        if len(args) >= 2 and args[1] == "confirm":
            pend = (ctx.user_data or {}).get(_TPS_PENDING)
            if not pend or (time.time() - pend["ts"]) > _FIRE_TTL_SEC:
                await reply("Nothing pending (or it expired). Start with "
                            "/autotrade tps <coin>.")
                return
            ctx.user_data.pop(_TPS_PENDING, None)
            await reply(f"Placing TP ladder on {pend['coin']}...")
            await self._engine.apply_tps(
                uid, pair=pend["pair"], take_profits=pend["tps"],
            )
            return

        if len(args) < 2:
            await reply("Usage: /autotrade tps <coin> [px1 px2 px3]\n"
                        "e.g. /autotrade tps JUP  (uses the signal's targets)")
            return
        coin = args[1].upper()
        pair = f"{coin}/USDT"

        # --- resolve TP prices: explicit args beat the signal lookup ---
        tps: list[float] = []
        if len(args) > 2:
            try:
                tps = [float(a) for a in args[2:5]]
            except ValueError:
                await reply("TP prices must be numbers, e.g. "
                            "/autotrade tps JUP 0.2006 0.194 0.1832")
                return
        elif self._open_signals_db is not None and self._signal_channel_id:
            sig = await self._open_signals_db.find_latest_open(
                channel_id=self._signal_channel_id, pair_or_base=coin,
            )
            if sig is not None:
                tps = [float(t) for t in (sig.tp1, sig.tp2, sig.tp3)
                       if t is not None]
        if not tps:
            await reply(
                f"No open {coin} signal with TP targets found. Give prices "
                f"explicitly: /autotrade tps {coin} <px1> [px2] [px3]"
            )
            return

        if ctx.user_data is not None:
            ctx.user_data[_TPS_PENDING] = {
                "pair": pair, "coin": coin, "tps": tps, "ts": time.time(),
            }
        weights = "/".join(
            f"{w:g}" for w in self._config.autotrade.tp_split_weights[: len(tps)]
        )
        await reply(
            f"<b>Confirm TP ladder</b>\n"
            f"{coin}: split your OPEN position {weights} across "
            + " / ".join(f"{t:g}" for t in tps)
            + " (reduce-only).\n\n"
            "Reply <code>/autotrade tps confirm</code> within 2 minutes to place.",
            parse_mode="HTML",
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
            f"Global: {'on' if at.enabled else 'off'} - {mode} - {at.venue}/{at.network}",
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

        if self._venue_name() == "blofin":
            label = await self._store_blofin(uid, text, update)
        else:
            label = await self._store_hyperliquid(uid, text, update)
        if label is None:
            return  # a parse/store error already replied to the user

        ctx.user_data.pop(_STATE_KEY, None)
        try:
            await update.effective_message.delete()
        except Exception:
            logger.debug("could not delete connect paste for user=%d", uid)

        await ctx.bot.send_message(
            chat_id=uid,
            text=(
                "<b>Connected.</b>\n"
                f"{label}\n\n"
                "Now enable it: <code>/autotrade on</code>, then "
                "<code>/autotrade agree</code>."
            ),
            parse_mode="HTML",
        )
        logger.info("autotrade connected for user=%d", uid)

    async def _store_hyperliquid(self, uid, text, update) -> str | None:
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
            return None
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
            return None
        return f"Account: <code>{address}</code>"

    async def _store_blofin(self, uid, text, update) -> str | None:
        key_m = _BLOFIN_KEY_RE.search(text)
        secret_m = _BLOFIN_SECRET_RE.search(text)
        pass_m = _BLOFIN_PASS_RE.search(text)
        if not key_m or not secret_m or not pass_m:
            await update.effective_message.reply_text(
                "I need three lines:\n"
                "<code>key: yourApiKey</code>\n"
                "<code>secret: yourApiSecret</code>\n"
                "<code>passphrase: yourPassphrase</code>\n"
                "Try again, or send /cancel.",
                parse_mode="HTML",
            )
            return None
        if self._blofin_creds_db is None:
            await update.effective_message.reply_text(
                "Blofin connect is not available on this bot."
            )
            return None
        try:
            await self._blofin_creds_db.upsert(
                telegram_user_id=uid,
                api_key=key_m.group(1),
                api_secret=secret_m.group(1),
                passphrase=pass_m.group(1),
            )
        except Exception:
            logger.exception("blofin connect store failed for user=%d", uid)
            await update.effective_message.reply_text(
                "Something went wrong storing your credentials. Nothing was "
                "saved. Try /autotrade connect again."
            )
            return None
        masked = key_m.group(1)[:4] + "..." + key_m.group(1)[-2:]
        return f"Blofin key: <code>{masked}</code>"
