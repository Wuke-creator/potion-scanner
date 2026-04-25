"""Router — classify each incoming Discord message and forward to Telegram.

The router is the only place where business logic lives:

  1. Look up the source channel route (gives us the ref link + source type).
  2. Classify the message via the parser.
  3. If it's a structured TRADING SIGNAL ALERT, parse fully and format with
     all fields populated.
  4. If it's a known lifecycle event (TP hit, breakeven, etc.), forward as
     a "trade update" with the event label.
  5. If it's NOISE or PREPARATION, drop it (don't spam the Elite group).
  6. If it doesn't match any known type but the channel allows free-form
     messages (Manual Perp Calls, Prediction Calls), forward verbatim.
  7. Send the formatted message via the broadcaster.

The router never raises — every error is logged and the message is dropped.
This keeps the listener loop alive even when the parser hits an unexpected
format.
"""

from __future__ import annotations

import logging

from src.analytics import AnalyticsDB
from src.config import ChannelRoute, DiscordConfig
from src.discord_listener import IncomingMessage
from src.dispatcher import Dispatcher
from src.formatter import (
    build_signal_keyboard,
    build_wallet_tracker_keyboard,
    format_lifecycle_event,
    format_parsed_signal,
    format_unknown_message,
    format_wallet_tracker_alert,
    label_for_source_type,
)
from src.parser import MessageType, SignalParseError, classify, parse_signal
from src.parser.wallet_tracker_parser import parse_wallet_tracker
from src.automations.wallet_tracker_debouncer import WalletTrackerDebouncer
from src.parser.update_parser import (
    UpdateParseError,
    parse_all_tp_hit,
    parse_breakeven,
    parse_canceled,
    parse_stop_hit,
    parse_tp_hit,
    parse_trade_closed,
)

logger = logging.getLogger(__name__)


# Channels where we forward unrecognized messages verbatim (humans posting).
# In these channels, NOISE-classified messages are still forwarded — the
# classifier was tuned for the Potion Perps Bot template, not human posts.
_FREEFORM_SOURCE_TYPES = {"memecoin"}  # prediction & memecoin spot calls


# Lifecycle events we DO forward (with a friendly label)
_LIFECYCLE_LABELS: dict[MessageType, str] = {
    MessageType.TP_HIT: "Take Profit Hit",
    MessageType.ALL_TP_HIT: "All Take Profits Hit",
    MessageType.BREAKEVEN: "Stop Loss to Breakeven",
    MessageType.STOP_HIT: "Stop Loss Hit",
    MessageType.TRADE_CLOSED: "Trade Closed",
    MessageType.MANUAL_UPDATE: "Manual Update",
    MessageType.CANCELED: "Trade Canceled",
}

# Always-dropped: PREPARATION posts (just teasers, not actionable)
_ALWAYS_DROP = {MessageType.PREPARATION}


class Router:
    """Routes parsed Discord messages to the DM fan-out dispatcher."""

    def __init__(
        self,
        discord_cfg: DiscordConfig,
        dispatcher: Dispatcher,
        analytics: AnalyticsDB | None = None,
    ):
        self._discord_cfg = discord_cfg
        self._dispatcher = dispatcher
        self._analytics = analytics
        # Debouncer for rapid same-trader same-token same-action buys on
        # the Wallet Tracker channel. Instantiated lazily (needs a stable
        # emit callback bound to this router instance).
        self._wallet_debouncer = WalletTrackerDebouncer(
            emit_fn=self._emit_wallet_tracker_alert,
            idle_timeout_sec=30.0,
            max_hold_sec=120.0,
        )

    async def _emit_wallet_tracker_alert(
        self, alert, count: int,
    ) -> None:
        """Debouncer callback: format + dispatch a (possibly consolidated)
        Wallet Tracker alert. Looks up the channel config at emit time
        since debouncer doesn't carry channel state across batches."""
        # Resolve wallet_tracker route for channel_name + source URL
        route = self._discord_cfg.channel_by_key("wallet_tracker")
        channel_name = route.name if route else "Wallet Tracker"
        channel_id = route.channel_id if route else 0
        source_url = self._build_discord_channel_url(channel_id)
        text = format_wallet_tracker_alert(
            alert=alert,
            channel_name=channel_name,
            source_url=source_url,
            count=count,
        )
        keyboard = build_wallet_tracker_keyboard(ca=alert.ca)
        await self._dispatcher.dispatch(
            text=text,
            source_key="wallet_tracker",
            pair="",
            keyboard=keyboard,
        )

    async def handle(self, message: IncomingMessage) -> None:
        """Process one incoming message end to end. Never raises."""
        try:
            await self._handle(message)
        except Exception:
            logger.exception(
                "Router crashed on message from channel=%d", message.channel_id,
            )

    def _build_discord_channel_url(self, channel_id: int) -> str:
        """Deep link back to the source Discord channel. Returns empty
        string when guild_id isn't configured (defensive — URL is optional
        in the formatters)."""
        guild_id = getattr(self._discord_cfg, "guild_id", 0)
        if not guild_id or not channel_id:
            return ""
        return f"https://discord.com/channels/{guild_id}/{channel_id}"

    async def _handle(self, message: IncomingMessage) -> None:
        route = self._discord_cfg.channel_by_id(message.channel_id)
        if route is None:
            logger.debug(
                "Message from unmonitored channel %d — ignored", message.channel_id,
            )
            return

        # Mirror mode: pass the message body through to Telegram unchanged.
        # No classification, no parsing, no header/footer wrap, no ref link
        # appended, no keyboard. For channels whose format doesn't fit the
        # structured perp or memecoin templates (e.g. third-party alert bots).
        if route.source_type == "mirror":
            logger.info(
                "Mirror-forwarding %d chars from #%s",
                len(message.content), route.name,
            )
            await self._dispatcher.dispatch(
                text=message.content,
                source_key=route.key,
                pair="",
                keyboard=None,
            )
            return

        # Wallet Tracker channel uses a dedicated parser+formatter that
        # produces a clean, structured alert (matching the calls-channel
        # visual style) and a single Trade-on-Terminal button pointing
        # at the token's Padre page with Orangie's ref code. Competitor
        # brand links (GMGN, AXIOM, BonkBot, etc.) are stripped out.
        if route.key == "wallet_tracker":
            # Drop non-trading wallet activity types. Onsight emits:
            #   BUY / SELL -> real trading signals, forward
            #   TRANSFER -> wallet-to-wallet movement, not a trade
            #   UNKNOWN -> Onsight's catch-all for activity it couldn't
            #     classify (often just balance changes or noise)
            # Only BUY/SELL should reach subscribers.
            head = (message.content or "").split("\n", 1)[0].upper()
            if "TRANSFER" in head or "UNKNOWN" in head:
                logger.info(
                    "Skipping non-trading Wallet Tracker alert (head=%r)",
                    head[:80],
                )
                return
            try:
                alert = parse_wallet_tracker(message.content)
            except Exception:
                logger.exception("Wallet tracker parse crashed; falling back")
                alert = None
            if alert is not None and alert.parsed_ok:
                logger.info(
                    "Wallet-tracker structured: action=%s token=%s trader=%s",
                    alert.action, alert.token, alert.trader,
                )
                # Hand to the debouncer instead of dispatching directly.
                # Rapid same-trader/same-token/same-action events get
                # consolidated into a single '(×N buys)' alert.
                await self._wallet_debouncer.add(alert)
                return
            # Parse failed (unknown format) → fall through to the normal
            # memecoin freeform path so we still forward something.
            logger.info(
                "Wallet-tracker parse incomplete (parsed_ok=False) — "
                "falling back to verbatim forward"
            )

        msg_type = classify(message.content)
        logger.info(
            "Classified message from #%s as %s", route.name, msg_type.value,
        )

        # Record analytics for lifecycle events before dropping anything.
        # Signal alerts are recorded later inside _build_text once fully parsed.
        await self._record_lifecycle_event(msg_type, message.content, route)

        if msg_type in _ALWAYS_DROP:
            logger.debug("Dropping %s message from #%s", msg_type.value, route.name)
            return

        # NOISE drops only on perps channels (memecoin channels forward verbatim
        # because the classifier wasn't trained on human-written predictions).
        if msg_type == MessageType.NOISE and route.source_type not in _FREEFORM_SOURCE_TYPES:
            logger.debug("Dropping NOISE in non-freeform channel #%s", route.name)
            return

        result = await self._build_text(msg_type, message, route)
        if result is None:
            return

        text, pair, keyboard = result
        await self._dispatcher.dispatch(
            text=text, source_key=route.key, pair=pair, keyboard=keyboard,
        )
        logger.info("Enqueued %s from #%s for fan-out", msg_type.value, route.name)

    async def _build_text(
        self,
        msg_type: MessageType,
        message: IncomingMessage,
        route: ChannelRoute,
    ) -> tuple[str, str, object] | None:
        """Returns (text, pair, keyboard) or None to drop the message.

        ``pair`` is the token pair string (e.g. "ETH/USDT") used by the
        dispatcher for muted-token filtering. Empty string when unknown.
        ``keyboard`` is an InlineKeyboardMarkup or None.
        """
        source_label = label_for_source_type(route.source_type)

        if msg_type == MessageType.SIGNAL_ALERT:
            try:
                signal = parse_signal(message.content)
            except SignalParseError as e:
                logger.warning(
                    "Could not parse SIGNAL_ALERT from #%s (%s): forwarding raw",
                    route.name,
                    e,
                )
                text = format_unknown_message(
                    raw_message=message.content,
                    ref_link=route.ref_link,
                    channel_name=route.name,
                    source_type_label=source_label,
                )
                return (text, "", None)
            # Record the signal for analytics (idempotent on trade_id + channel)
            if self._analytics is not None:
                try:
                    await self._analytics.record_signal(
                        trade_id=signal.trade_id,
                        channel_key=route.key,
                        pair=signal.pair,
                        side=signal.side.value,
                        entry=signal.entry,
                        leverage=signal.leverage,
                    )
                except Exception:
                    logger.exception("Analytics: failed to record signal")
            text = format_parsed_signal(
                signal=signal,
                ref_link=route.ref_link,
                channel_name=route.name,
                source_type_label=source_label,
            )
            keyboard = build_signal_keyboard(
                ref_link=route.ref_link, pair=signal.pair,
            )
            return (text, signal.pair, keyboard)

        if msg_type in _LIFECYCLE_LABELS:
            text = format_lifecycle_event(
                label=_LIFECYCLE_LABELS[msg_type],
                raw_message=message.content,
                ref_link=route.ref_link,
                channel_name=route.name,
                source_type_label=source_label,
            )
            return (text, "", None)

        # Unknown / free-form fallback. Forward verbatim only for human-driven channels.
        if route.source_type in _FREEFORM_SOURCE_TYPES:
            text = format_unknown_message(
                raw_message=message.content,
                ref_link=route.ref_link,
                channel_name=route.name,
                source_type_label=source_label,
            )
            return (text, "", None)

        logger.debug(
            "Unrecognized message in non-freeform channel #%s: dropped", route.name,
        )
        return None

    async def _record_lifecycle_event(
        self,
        msg_type: MessageType,
        content: str,
        route: ChannelRoute,
    ) -> None:
        """Record a lifecycle event to analytics. Swallows parse errors."""
        if self._analytics is None:
            return

        trade_id: int | None = None
        event_type: str | None = None
        tp_number: int | None = None
        pnl_pct: float | None = None

        try:
            if msg_type == MessageType.TP_HIT:
                parsed = parse_tp_hit(content)
                trade_id = parsed.trade_id
                event_type = "tp_hit"
                tp_number = parsed.tp_number
                pnl_pct = parsed.profit_pct
            elif msg_type == MessageType.ALL_TP_HIT:
                parsed = parse_all_tp_hit(content)
                trade_id = parsed.trade_id
                event_type = "all_tp_hit"
                tp_number = 3
                pnl_pct = parsed.profit_pct
            elif msg_type == MessageType.BREAKEVEN:
                parsed = parse_breakeven(content)
                trade_id = parsed.trade_id
                event_type = "breakeven"
                tp_number = parsed.tp_secured
            elif msg_type == MessageType.STOP_HIT:
                parsed = parse_stop_hit(content)
                trade_id = parsed.trade_id
                event_type = "stop_hit"
                pnl_pct = parsed.loss_pct
            elif msg_type == MessageType.CANCELED:
                parsed = parse_canceled(content)
                trade_id = parsed.trade_id
                event_type = "canceled"
            elif msg_type == MessageType.TRADE_CLOSED:
                parsed = parse_trade_closed(content)
                trade_id = parsed.trade_id
                event_type = "trade_closed"
            else:
                return
        except UpdateParseError as e:
            logger.debug(
                "Analytics: skipping %s event (parse failed: %s)",
                msg_type.value, e,
            )
            return

        if trade_id is None or event_type is None:
            return

        try:
            await self._analytics.record_event(
                trade_id=trade_id,
                channel_key=route.key,
                event_type=event_type,
                tp_number=tp_number,
                pnl_pct=pnl_pct,
            )
        except Exception:
            logger.exception("Analytics: failed to record event %s", event_type)
