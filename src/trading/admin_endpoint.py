"""Admin-only HTTP endpoint for firing a synthetic signal at a specific user.

Used for smoke-testing the 1-Tap Trade flow + image forwarding + the
Perp Pinger paths without waiting for a real Discord post AND without
fanning out to all verified subscribers.

Mounts on the OAuth callback server's aiohttp app at:

  POST /admin/trading/test-signal
    Headers: X-Admin-Secret: <ADMIN_WEBHOOK_SECRET>
    Body: {
      "discord_user_id": "901091776977338419",
      "forward_as": "structured" | "perp_pinger_new" | "perp_pinger_update",
                       # defaults to "structured" (existing behavior)
      "pair": "BTC/USD" | "JUP" | "LAB",
      "side": "LONG" | "SHORT",   # required for structured + perp_pinger_new
      "leverage": 10,             # structured only
      "entry": 65000,             # required for structured + perp_pinger_new
      "stop_loss": 63500,         # optional for perp_pinger_new
      "tp1": 67000,               # optional for perp_pinger_new
      "tp2": 69000,               # structured only
      "tp3": 72000,               # structured only
      "risk_level": "MEDIUM",     # optional, defaults MEDIUM (structured only)
      "trade_id": 99999,          # optional, defaults to time-based int
      "include_quick_trade": true,# optional, defaults true

      # perp_pinger_update only:
      "update_kind": "close" | "stop" | "be" | "tp_hit" | "adjust",
      "raw_message": "Closing LAB here in full closed",

      # all forwards (optional):
      "image_url": "https://cdn.discordapp.com/...",
      "stop_loss_is_conditional": false,
      "stop_loss_raw": "4h candle close above 0.2470$"
    }
    -> 200 { ok: true, telegram_user_id, signal_id, message_id }
    -> 401 unauthorized
    -> 404 user not verified

Synthetic signals use channel_id=0 in open_signals_db so they can't
collide with real Discord channel IDs.
"""

from __future__ import annotations

import hmac
import logging
import math
import time
from typing import Any

from aiohttp import web
from telegram import Bot

from src.automations.open_signals_db import OpenSignalsDB
from src.config import Config
from src.formatter import (
    build_signal_keyboard,
    format_lifecycle_event,
    format_parsed_signal,
    format_perp_pinger_signal,
)
from src.parser.perp_pinger_parser import PerpPingerSignal
from src.parser.perp_pinger_update_parser import (
    LABELS as _PERP_PINGER_UPDATE_LABELS,
    UpdateKind as _PerpPingerUpdateKind,
)
from src.parser.signal_parser import ParsedSignal, RiskLevel, Side
from src.verification.db import VerificationDB

logger = logging.getLogger(__name__)


_SYNTHETIC_CHANNEL_ID = 0
_CAPTION_LIMIT = 1024


def _safe_float(value: Any) -> float:
    """Coerce ``value`` to a finite float or raise ValueError.

    Python's ``float()`` accepts "nan", "inf", "-inf" as valid inputs,
    and JSON's null deserialises to None. Both shapes can poison
    downstream price comparisons and DB rows. Reject them here.
    """
    f = float(value)
    if math.isnan(f) or math.isinf(f):
        raise ValueError(f"non-finite float: {value!r}")
    return f


def _safe_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return _safe_float(value)


class AdminTradingEndpoint:
    """aiohttp routes for admin testing of the 1-Tap Trade flow."""

    def __init__(
        self,
        *,
        config: Config,
        admin_secret: str,
        verification_db: VerificationDB,
        open_signals_db: OpenSignalsDB,
        telegram_bot: Bot,
    ):
        self._config = config
        self._admin_secret = admin_secret
        self._verification_db = verification_db
        self._open_signals_db = open_signals_db
        self._telegram_bot = telegram_bot

    def register(self, app: web.Application) -> None:
        app.router.add_post(
            "/admin/trading/test-signal", self._handle_test_signal,
        )

    def _authorized(self, request: web.Request) -> bool:
        if not self._admin_secret:
            return False
        given = request.headers.get("X-Admin-Secret", "").strip()
        return hmac.compare_digest(given, self._admin_secret)

    async def _send(
        self,
        *,
        chat_id: int,
        text: str,
        keyboard,
        image_url: str | None,
    ):
        """Send via send_photo when image_url is set, with caption-overflow
        fallback. Otherwise send_message. Returns the Telegram Message."""
        if image_url:
            if len(text) <= _CAPTION_LIMIT:
                return await self._telegram_bot.send_photo(
                    chat_id=chat_id,
                    photo=image_url,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            else:
                await self._telegram_bot.send_photo(
                    chat_id=chat_id, photo=image_url, caption="",
                )
                return await self._telegram_bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=keyboard,
                )
        return await self._telegram_bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )

    async def _handle_test_signal(
        self, request: web.Request,
    ) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)

        forward_as = str(body.get("forward_as", "structured")).lower().strip()
        if forward_as not in ("structured", "perp_pinger_new", "perp_pinger_update"):
            return web.json_response(
                {"error": "forward_as must be structured | perp_pinger_new | perp_pinger_update"},
                status=400,
            )

        try:
            discord_user_id = str(body["discord_user_id"]).strip()
        except KeyError as e:
            return web.json_response({"error": f"missing: {e}"}, status=400)

        user = await self._verification_db.get_by_discord_user_id(discord_user_id)
        if user is None or not user.is_active:
            return web.json_response(
                {"error": "user not verified or inactive"}, status=404,
            )

        image_url = body.get("image_url") or None
        include_quick_trade = bool(body.get("include_quick_trade", True))
        ref_link = "https://app.ostium.com/?ref=PTION"

        if forward_as == "structured":
            return await self._send_structured(
                body=body,
                user=user,
                discord_user_id=discord_user_id,
                image_url=image_url,
                include_quick_trade=include_quick_trade,
                ref_link=ref_link,
            )
        if forward_as == "perp_pinger_new":
            return await self._send_perp_pinger_new(
                body=body,
                user=user,
                discord_user_id=discord_user_id,
                image_url=image_url,
                include_quick_trade=include_quick_trade,
                ref_link=ref_link,
            )
        # perp_pinger_update
        return await self._send_perp_pinger_update(
            body=body,
            user=user,
            discord_user_id=discord_user_id,
            image_url=image_url,
            ref_link=ref_link,
        )

    # ---- structured (legacy / Potion Perps Bot template) -------------------

    async def _send_structured(
        self, *, body, user, discord_user_id, image_url, include_quick_trade, ref_link,
    ) -> web.Response:
        try:
            pair = str(body["pair"]).strip()
            side_str = str(body["side"]).strip().upper()
            leverage = int(body["leverage"])
            entry = _safe_float(body["entry"])
            stop_loss = _safe_float(body["stop_loss"])
            tp1 = _safe_float(body["tp1"])
            tp2 = _safe_float(body["tp2"])
            tp3 = _safe_float(body["tp3"])
        except (KeyError, TypeError, ValueError) as e:
            return web.json_response({"error": f"bad_request: {e}"}, status=400)
        if side_str not in ("LONG", "SHORT"):
            return web.json_response(
                {"error": "side must be LONG or SHORT"}, status=400,
            )
        risk_str = str(body.get("risk_level", "MEDIUM")).upper()
        if risk_str not in ("LOW", "MEDIUM", "HIGH"):
            risk_str = "MEDIUM"
        trade_id = int(body.get("trade_id", int(time.time()) % 1_000_000))

        open_signal_id = await self._open_signals_db.record_signal(
            channel_id=_SYNTHETIC_CHANNEL_ID,
            pair=pair, side=side_str, leverage=leverage,
            entry=entry, stop_loss=stop_loss, tp1=tp1, tp2=tp2, tp3=tp3,
            trade_id=trade_id, raw_message="(admin test: structured)",
        )

        parsed = ParsedSignal(
            pair=pair, trade_id=trade_id, risk_level=RiskLevel[risk_str],
            trade_type="SCALP", size="1-2%", side=Side[side_str],
            entry=entry, stop_loss=stop_loss, tp1=tp1, tp2=tp2, tp3=tp3,
            leverage=leverage,
        )
        text = format_parsed_signal(
            signal=parsed, ref_link=ref_link,
            channel_name="Test Signal", source_type_label="PERPS",
        )
        keyboard = build_signal_keyboard(
            ref_link=ref_link, pair=pair,
            quick_trade_signal_id=(
                open_signal_id
                if include_quick_trade and self._config.trading.enabled
                else None
            ),
        )

        return await self._send_and_respond(
            user=user, discord_user_id=discord_user_id,
            text=text, keyboard=keyboard, image_url=image_url,
            open_signal_id=open_signal_id,
        )

    # ---- perp_pinger_new (free-form new-call) ------------------------------

    async def _send_perp_pinger_new(
        self, *, body, user, discord_user_id, image_url, include_quick_trade, ref_link,
    ) -> web.Response:
        try:
            pair = str(body["pair"]).strip().upper()
            side_str = str(body["side"]).strip().upper()
            entry = _safe_float(body["entry"])
        except (KeyError, TypeError, ValueError) as e:
            return web.json_response({"error": f"bad_request: {e}"}, status=400)
        if side_str not in ("LONG", "SHORT"):
            return web.json_response(
                {"error": "side must be LONG or SHORT"}, status=400,
            )

        try:
            stop_loss = _safe_optional_float(body.get("stop_loss"))
            take_profit = _safe_optional_float(
                body.get("tp1") if body.get("tp1") is not None else body.get("take_profit")
            )
            risk_rr = _safe_optional_float(body.get("risk_rr"))
        except (TypeError, ValueError) as e:
            return web.json_response({"error": f"bad_request: {e}"}, status=400)
        stop_loss_is_conditional = bool(body.get("stop_loss_is_conditional", False))
        stop_loss_raw = body.get("stop_loss_raw")

        open_signal_id = await self._open_signals_db.record_signal(
            channel_id=_SYNTHETIC_CHANNEL_ID,
            pair=pair, side=side_str, leverage=None,
            entry=entry, stop_loss=stop_loss,
            tp1=take_profit, tp2=None, tp3=None,
            trade_id=None,
            raw_message="(admin test: perp_pinger_new)",
            stop_loss_is_conditional=stop_loss_is_conditional,
        )

        parsed = PerpPingerSignal(
            pair=pair, side=side_str, entry=entry,
            stop_loss=stop_loss,
            stop_loss_is_conditional=stop_loss_is_conditional,
            stop_loss_raw=stop_loss_raw,
            take_profit=take_profit, risk_rr=risk_rr,
        )
        text = format_perp_pinger_signal(
            parsed=parsed, ref_link=ref_link,
            channel_name="Test Signal", source_type_label="PERPS",
        )
        keyboard = build_signal_keyboard(
            ref_link=ref_link, pair=pair,
            quick_trade_signal_id=(
                open_signal_id
                if include_quick_trade and self._config.trading.enabled
                else None
            ),
        )

        return await self._send_and_respond(
            user=user, discord_user_id=discord_user_id,
            text=text, keyboard=keyboard, image_url=image_url,
            open_signal_id=open_signal_id,
        )

    # ---- perp_pinger_update (free-form close/stop/BE/TP-hit) ---------------

    async def _send_perp_pinger_update(
        self, *, body, user, discord_user_id, image_url, ref_link,
    ) -> web.Response:
        try:
            pair = str(body["pair"]).strip().upper()
            kind_str = str(body["update_kind"]).strip().lower()
        except (KeyError, TypeError) as e:
            return web.json_response({"error": f"bad_request: {e}"}, status=400)

        try:
            kind = _PerpPingerUpdateKind(kind_str)
        except ValueError:
            return web.json_response(
                {"error": f"update_kind must be one of "
                          f"{[k.value for k in _PerpPingerUpdateKind]}"},
                status=400,
            )

        raw_message = body.get("raw_message") or f"Manual {kind.value} on {pair}"

        original_signal = await self._open_signals_db.find_latest_open(
            channel_id=_SYNTHETIC_CHANNEL_ID, pair_or_base=pair,
        )
        label = _PERP_PINGER_UPDATE_LABELS.get(kind, "Manual Update")
        text = format_lifecycle_event(
            label=label, raw_message=raw_message, ref_link=ref_link,
            channel_name="Test Signal", source_type_label="PERPS",
            original_signal=original_signal,
        )
        keyboard = build_signal_keyboard(
            ref_link=ref_link, pair=pair, quick_trade_signal_id=None,
        )

        original_signal_id = (
            original_signal.id if original_signal is not None else None
        )
        return await self._send_and_respond(
            user=user, discord_user_id=discord_user_id,
            text=text, keyboard=keyboard, image_url=image_url,
            open_signal_id=original_signal_id,
        )

    # ---- shared send + JSON response builder -------------------------------

    async def _send_and_respond(
        self, *, user, discord_user_id, text, keyboard,
        image_url, open_signal_id,
    ) -> web.Response:
        try:
            message = await self._send(
                chat_id=user.telegram_user_id,
                text=text,
                keyboard=keyboard,
                image_url=image_url,
            )
        except Exception as e:
            logger.exception(
                "Admin test-signal send failed for discord=%s tg=%d",
                discord_user_id, user.telegram_user_id,
            )
            return web.json_response(
                {"error": f"send_failed: {e}"}, status=500,
            )

        logger.info(
            "Admin test-signal sent: discord=%s tg=%d signal_id=%s",
            discord_user_id, user.telegram_user_id, open_signal_id,
        )
        return web.json_response({
            "ok": True,
            "telegram_user_id": user.telegram_user_id,
            "signal_id": open_signal_id,
            "message_id": message.message_id,
        })
