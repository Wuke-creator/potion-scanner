"""Admin-only HTTP endpoint for firing a synthetic signal at a specific user.

Used for smoke-testing the 1-Tap Trade flow without waiting for a real
perp signal AND without fanning out to all verified subscribers.

Mounts on the OAuth callback server's aiohttp app at:

  POST /admin/trading/test-signal
    Headers: X-Admin-Secret: <ADMIN_WEBHOOK_SECRET>
    Body: {
      "discord_user_id": "901091776977338419",
      "pair": "BTC/USD",
      "side": "LONG",          // or "SHORT"
      "leverage": 10,
      "entry": 65000,
      "stop_loss": 63500,
      "tp1": 67000,
      "tp2": 69000,
      "tp3": 72000,
      "risk_level": "MEDIUM",  // optional, defaults MEDIUM
      "trade_id": 99999,       // optional, defaults to time-based int
      "include_quick_trade": true  // optional, defaults true
    }
    -> 200 { ok: true, telegram_user_id, signal_id, message_id }
    -> 401 unauthorized
    -> 404 user not verified

Synthetic signals get channel_id=0 in open_signals_db so they cannot
collide with real Discord channel IDs.
"""

from __future__ import annotations

import hmac
import logging
import time
from typing import Any

from aiohttp import web
from telegram import Bot

from src.automations.open_signals_db import OpenSignalsDB
from src.config import Config
from src.formatter import build_signal_keyboard, format_parsed_signal
from src.parser.signal_parser import ParsedSignal, RiskLevel, Side
from src.verification.db import VerificationDB

logger = logging.getLogger(__name__)


_SYNTHETIC_CHANNEL_ID = 0


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

    async def _handle_test_signal(
        self, request: web.Request,
    ) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)

        try:
            discord_user_id = str(body["discord_user_id"]).strip()
            pair = str(body["pair"]).strip()
            side_str = str(body["side"]).strip().upper()
            leverage = int(body["leverage"])
            entry = float(body["entry"])
            stop_loss = float(body["stop_loss"])
            tp1 = float(body["tp1"])
            tp2 = float(body["tp2"])
            tp3 = float(body["tp3"])
        except (KeyError, TypeError, ValueError) as e:
            return web.json_response(
                {"error": f"bad_request: {e}"}, status=400,
            )

        if side_str not in ("LONG", "SHORT"):
            return web.json_response(
                {"error": "side must be LONG or SHORT"}, status=400,
            )
        risk_str = str(body.get("risk_level", "MEDIUM")).upper()
        if risk_str not in ("LOW", "MEDIUM", "HIGH"):
            risk_str = "MEDIUM"
        trade_id = int(body.get("trade_id", int(time.time()) % 1_000_000))
        include_quick_trade = bool(body.get("include_quick_trade", True))

        user = await self._verification_db.get_by_discord_user_id(discord_user_id)
        if user is None or not user.is_active:
            return web.json_response(
                {"error": "user not verified or inactive"}, status=404,
            )

        open_signal_id = await self._open_signals_db.record_signal(
            channel_id=_SYNTHETIC_CHANNEL_ID,
            pair=pair,
            side=side_str,
            leverage=leverage,
            entry=entry,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            trade_id=trade_id,
            raw_message="(admin test signal)",
        )

        parsed = ParsedSignal(
            pair=pair,
            trade_id=trade_id,
            risk_level=RiskLevel[risk_str],
            trade_type="SCALP",
            size="1-2%",
            side=Side[side_str],
            entry=entry,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            leverage=leverage,
        )

        ref_link = "https://app.ostium.com/?ref=PTION"
        text = format_parsed_signal(
            signal=parsed,
            ref_link=ref_link,
            channel_name="Test Signal",
            source_type_label="PERPS",
        )
        keyboard = build_signal_keyboard(
            ref_link=ref_link,
            pair=pair,
            quick_trade_signal_id=(
                open_signal_id
                if include_quick_trade and self._config.trading.enabled
                else None
            ),
        )

        try:
            message = await self._telegram_bot.send_message(
                chat_id=user.telegram_user_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
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
            "Admin test-signal sent: discord=%s tg=%d signal_id=%d",
            discord_user_id, user.telegram_user_id, open_signal_id,
        )
        return web.json_response({
            "ok": True,
            "telegram_user_id": user.telegram_user_id,
            "signal_id": open_signal_id,
            "message_id": message.message_id,
            "quick_trade_button": (
                include_quick_trade and self._config.trading.enabled
            ),
        })
