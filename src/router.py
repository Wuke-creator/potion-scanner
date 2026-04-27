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
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.analytics import AnalyticsDB
from src.config import ChannelRoute, DiscordConfig
from src.discord_listener import IncomingMessage
from src.dispatcher import Dispatcher
from src.formatter import (
    _extract_pair_from_caption,
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
from src.parser.image_ocr import ocr_available, ocr_image_url, parse_ocr_text
from src.automations.open_signals_db import OpenSignalsDB
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

# Status flips applied to the open_signals memory layer when a lifecycle
# event lands. Used to mark trades as terminated so future events for the
# same symbol don't accidentally pull in a closed trade as 'original'.
_LIFECYCLE_TERMINAL_STATUSES: dict[MessageType, str] = {
    MessageType.ALL_TP_HIT: "all_tp_hit",
    MessageType.STOP_HIT: "stopped",
    MessageType.TRADE_CLOSED: "closed",
    MessageType.CANCELED: "canceled",
}
_LIFECYCLE_NON_TERMINAL_STATUSES: dict[MessageType, str] = {
    MessageType.TP_HIT: "tp_hit",
    MessageType.BREAKEVEN: "breakeven",
}

# Always-dropped: PREPARATION posts (just teasers, not actionable)
_ALWAYS_DROP = {MessageType.PREPARATION}

# Telegram inline keyboards have a hard cap of 8 buttons per row and
# 100 buttons total. We send at most 4 buttons per row of mirrored
# Discord component buttons so labels don't get truncated on mobile.
_MAX_BUTTONS_PER_ROW = 4


class Router:
    """Routes parsed Discord messages to the DM fan-out dispatcher."""

    def __init__(
        self,
        discord_cfg: DiscordConfig,
        dispatcher: Dispatcher,
        analytics: AnalyticsDB | None = None,
        open_signals: OpenSignalsDB | None = None,
    ):
        self._discord_cfg = discord_cfg
        self._dispatcher = dispatcher
        self._analytics = analytics
        # Open-signals memory: when present, every parsed new signal gets
        # recorded here, and lifecycle events look up their original
        # signal so we can render entry/SL/TP context that the update
        # message itself doesn't carry. None disables the feature
        # gracefully (image-bot updates still forward, just without the
        # 'From the original call' block).
        self._open_signals = open_signals
        # Debouncer for rapid same-trader same-token same-action buys on
        # the Wallet Tracker channel. Instantiated lazily (needs a stable
        # emit callback bound to this router instance).
        self._wallet_debouncer = WalletTrackerDebouncer(
            emit_fn=self._emit_wallet_tracker_alert,
            idle_timeout_sec=10.0,
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

        # Mirror mode: pass the message body through to Telegram unchanged,
        # plus carry over any URL buttons attached to the original Discord
        # post (Onsight's "Trade via Onsight" / "Mobile Waitlist" CTAs on
        # Bonds, etc.). Mirror channels are for third-party alert bots
        # whose format doesn't fit the structured perp or memecoin
        # templates — we want the Telegram subscriber to see exactly what
        # the Discord post showed, including its one-tap action buttons.
        if route.source_type == "mirror":
            keyboard = self._build_mirror_keyboard(message.buttons or [])
            logger.info(
                "Mirror-forwarding %d chars + %d button(s) from #%s",
                len(message.content),
                len(message.buttons or []),
                route.name,
            )
            await self._dispatcher.dispatch(
                text=message.content,
                source_key=route.key,
                pair="",
                keyboard=keyboard,
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

        # Image-OCR new-signal fallback. When an image-bot channel posts a
        # chart-card-only signal (caption is empty or just decorative), the
        # caption can't be classified, so the message would normally drop.
        # OCR the first attached image; if it yields enough fields to
        # qualify as a new signal, record it in the memory layer and
        # dispatch a synthesised alert. Cheap on memory layer (one row),
        # only fires when the rest of the pipeline can't handle the post.
        image_urls = message.image_urls or []
        if image_urls and ocr_available():
            handled = await self._try_ocr_new_signal(message, route, image_urls)
            if handled:
                return

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

        # After successful dispatch, flip the open_signals status for
        # terminal lifecycle events so future events don't pull the
        # closed trade as 'original context'.
        await self._maybe_flip_open_signal_status(msg_type, message, route)

    async def _try_ocr_new_signal(
        self,
        message: IncomingMessage,
        route: ChannelRoute,
        image_urls: list[str],
    ) -> bool:
        """Attempt to extract a new signal from an attached chart image.

        Returns True if the OCR pipeline produced a viable signal AND we
        dispatched a Telegram alert (caller should stop further routing).
        Returns False if OCR found nothing usable; caller continues with
        the normal classify+route path.

        Skips OCR when the caption already looks like a structured perp-
        bot signal — those parse fine without OCR and we shouldn't
        double-process them.
        """
        text = message.content or ""
        upper = text.upper()
        # Bail fast if the existing path will handle it.
        if "TRADING SIGNAL ALERT" in upper or "TP TARGET" in upper:
            return False
        # Captions that obviously represent a lifecycle event also get
        # handled by the normal path (with open_signals enrichment).
        for marker in ("TP1", "TP 1", "TP HIT", "STOP HIT", "MOVE SL", "BREAKEVEN", "CANCELED", "CLOSED"):
            if marker in upper:
                return False

        ocr_text = await ocr_image_url(image_urls[0])
        if not ocr_text:
            return False
        fields = parse_ocr_text(ocr_text)
        base = fields.get("base")
        if not base:
            return False
        # Treat as new signal only when we have at least entry + (sl OR tp1).
        # Update-style cards (ROI/market/etc. without entry+SL) get left
        # for the regular caption path.
        if "entry" not in fields:
            return False
        if "stop_loss" not in fields and "tp1" not in fields:
            return False

        pair = fields.get("pair") or base
        side = fields.get("side")
        leverage = fields.get("leverage")
        entry = fields.get("entry")
        sl = fields.get("stop_loss")
        tp1 = fields.get("tp1")
        tp2 = fields.get("tp2")
        tp3 = fields.get("tp3")

        # Record into open_signals so future lifecycle events for this
        # symbol can be enriched.
        if self._open_signals is not None:
            try:
                await self._open_signals.record_signal(
                    channel_id=message.channel_id,
                    pair=pair,
                    side=side,
                    leverage=leverage,
                    entry=entry,
                    stop_loss=sl,
                    tp1=tp1,
                    tp2=tp2,
                    tp3=tp3,
                    trade_id=None,
                    raw_message=text + "\n\n[OCR]\n" + ocr_text,
                )
            except Exception:
                logger.exception("OCR signal record_signal failed")

        # Build a synthesised "new call" Telegram alert from the OCR
        # fields. We use format_unknown_message as the carrier and
        # prepend a clean structured block — keeps formatting consistent
        # with the rest of the perp pipeline.
        synthesized_lines: list[str] = []
        head_bits: list[str] = [str(pair)]
        if side:
            head_bits.append(side.upper())
        if leverage:
            head_bits.append(f"{int(leverage)}x")
        synthesized_lines.append(" ".join(head_bits))
        if entry is not None:
            synthesized_lines.append(f"Entry: {entry}")
        if sl is not None:
            synthesized_lines.append(f"SL: {sl}")
        if tp1 is not None:
            synthesized_lines.append(f"TP1: {tp1}")
        if tp2 is not None:
            synthesized_lines.append(f"TP2: {tp2}")
        if tp3 is not None:
            synthesized_lines.append(f"TP3: {tp3}")
        synthesized = "\n".join(synthesized_lines)
        # Preserve the original caption underneath so context isn't lost.
        if text:
            synthesized = synthesized + "\n\n" + text

        alert_text = format_unknown_message(
            raw_message=synthesized,
            ref_link=route.ref_link,
            channel_name=route.name,
            source_type_label=label_for_source_type(route.source_type),
        )
        keyboard = build_signal_keyboard(
            ref_link=route.ref_link, pair=pair,
        )
        await self._dispatcher.dispatch(
            text=alert_text, source_key=route.key, pair=pair, keyboard=keyboard,
        )
        logger.info(
            "OCR new-signal dispatched: pair=%s side=%s entry=%s",
            pair, side, entry,
        )
        return True

    async def _maybe_flip_open_signal_status(
        self,
        msg_type: MessageType,
        message: IncomingMessage,
        route: ChannelRoute,
    ) -> None:
        """If a lifecycle event landed for a known open signal, mark its
        new status. No-op when memory layer is disabled or no match
        exists. Cheap (one indexed UPDATE)."""
        if self._open_signals is None:
            return
        new_status = (
            _LIFECYCLE_TERMINAL_STATUSES.get(msg_type)
            or _LIFECYCLE_NON_TERMINAL_STATUSES.get(msg_type)
        )
        if not new_status:
            return
        ticker = self._extract_pair_or_ticker(message.content)
        if not ticker:
            return
        try:
            await self._open_signals.update_status(
                channel_id=message.channel_id,
                pair_or_base=ticker,
                new_status=new_status,
            )
        except Exception:
            logger.exception("open_signals.update_status crashed")

    @staticmethod
    def _build_mirror_keyboard(
        buttons: list[tuple[str, str]],
    ) -> InlineKeyboardMarkup | None:
        """Translate Discord URL buttons into a Telegram inline keyboard.

        Returns None when no buttons were captured so the dispatcher
        sends without a keyboard (Telegram tolerates empty rows but it
        looks visually awkward in the chat). Lays out at most
        ``_MAX_BUTTONS_PER_ROW`` buttons per row to keep labels readable
        on mobile.
        """
        if not buttons:
            return None
        rows: list[list[InlineKeyboardButton]] = []
        current: list[InlineKeyboardButton] = []
        for label, url in buttons:
            current.append(InlineKeyboardButton(text=label, url=url))
            if len(current) >= _MAX_BUTTONS_PER_ROW:
                rows.append(current)
                current = []
        if current:
            rows.append(current)
        return InlineKeyboardMarkup(rows) if rows else None

    @staticmethod
    def _is_empty_signal_pointer(content: str) -> bool:
        """Return True if a SIGNAL_ALERT-classified message has no actual
        signal data — just a header, mentions, and possibly a Discord URL
        pointing at another channel.

        Used to drop pointer-style posts like:

            Trading Signal Alert
            <@&1316518702790742059>
            https://discord.com/channels/1260259552763580537/.../...

        Without this check those forward as garbled "New Call Detected"
        messages on Telegram with no entry/SL/TPs, which is worse than
        no message at all.
        """
        if not content:
            return True
        # Strip Discord syntax that conveys no information. We match
        # BOTH the unescaped form (``<@&123>``) and the html-entity-
        # escaped form (``&lt;@&amp;123&gt;``) because content that
        # flowed through Discord embed serialisation has been escaped
        # by ``_serialize_embed``. Without the escaped-form match those
        # mentions survive here, the regex sees digits remaining,
        # decides 'this isn't a pointer', and forwards a garbage
        # alert to Telegram.
        cleaned = re.sub(r"<@&\d+>|&lt;@&amp;\d+&gt;", "", content)
        cleaned = re.sub(r"<@!?\d+>|&lt;@!?\d+&gt;", "", cleaned)
        cleaned = re.sub(r"<#\d+>|&lt;#\d+&gt;", "", cleaned)
        cleaned = re.sub(
            r"<a?:[A-Za-z0-9_]+:\d+>|&lt;a?:[A-Za-z0-9_]+:\d+&gt;",
            "",
            cleaned,
        )
        # Strip Discord message URLs (the "see signal in other channel"
        # pointer) — they're cosmetically a link but carry no parseable
        # signal fields.
        cleaned = re.sub(
            r"https?://(?:www\.)?discord\.com/channels/\d+/\d+(?:/\d+)?",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        # Strip the header phrase itself so it doesn't pad the content.
        cleaned = re.sub(
            r"trading\s+signal\s+alert", "", cleaned, flags=re.IGNORECASE,
        )
        cleaned = cleaned.strip()
        if not cleaned:
            return True
        # If anything remains, it should at minimum contain a digit for
        # this to be a real signal post. No digits => no entry / SL /
        # leverage / TPs => not a signal.
        return not re.search(r"\d", cleaned)

    @staticmethod
    def _extract_pair_or_ticker(content: str) -> str | None:
        """Extract a coin ticker from a free-form lifecycle caption.

        Delegates to ``formatter._extract_pair_from_caption`` so the
        router and the lifecycle Trade-Now URL builder share one
        ticker-detection implementation. Returns None if no candidate
        is found (callers handle the no-pair case).
        """
        ticker = _extract_pair_from_caption(content or "")
        return ticker or None

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
                # No actual call. The classifier matched the "Trading
                # Signal Alert" header but parse_signal failed and the
                # body has no entry / SL / TPs (after stripping role
                # pings, channel mentions, and pointer URLs). Forwarding
                # an empty header just confuses Telegram subscribers.
                if self._is_empty_signal_pointer(message.content):
                    logger.info(
                        "Dropping signal-less SIGNAL_ALERT from #%s "
                        "(role ping / pointer URL only, no fields).",
                        route.name,
                    )
                    return None
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
            # Record into the open_signals memory layer so subsequent
            # lifecycle events (TP1 hit, SL moved, etc.) can be enriched
            # with the original entry/SL/TP prices when the update post
            # itself is just a sparse caption.
            if self._open_signals is not None:
                try:
                    await self._open_signals.record_signal(
                        channel_id=message.channel_id,
                        pair=signal.pair,
                        side=signal.side.value,
                        leverage=signal.leverage,
                        entry=signal.entry,
                        stop_loss=signal.stop_loss,
                        tp1=signal.tp1,
                        tp2=signal.tp2,
                        tp3=signal.tp3,
                        trade_id=signal.trade_id,
                        raw_message=message.content,
                    )
                except Exception:
                    logger.exception("open_signals: failed to record signal")
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
            # Look up the originating signal so we can render the entry,
            # SL, and TP prices that the update message itself doesn't
            # carry (image-bot updates often only say "TP1 hit, move
            # SL to BE" — the actual numbers live in our memory layer
            # from the original signal post).
            original_signal = await self._lookup_original_for_lifecycle(
                msg_type, message, route,
            )
            text = format_lifecycle_event(
                label=_LIFECYCLE_LABELS[msg_type],
                raw_message=message.content,
                ref_link=route.ref_link,
                channel_name=route.name,
                source_type_label=source_label,
                original_signal=original_signal,
            )
            # Pair for the Trade-now button + dispatcher mute filter.
            # Prefer the memory-layer pair, fall back to extracting a
            # ticker from the caption so even cold-start updates still
            # get a working keyboard.
            pair_for_filter = (
                (original_signal.pair if original_signal else "")
                or self._extract_pair_or_ticker(message.content)
                or ""
            )
            keyboard = None
            if pair_for_filter:
                # Same Trade-now + Chart keyboard as new signals — keeps
                # the visual contract identical across the signal lifetime.
                keyboard = build_signal_keyboard(
                    ref_link=route.ref_link, pair=pair_for_filter,
                )
            return (text, pair_for_filter, keyboard)

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

    async def _lookup_original_for_lifecycle(
        self,
        msg_type: MessageType,
        message: IncomingMessage,
        route: ChannelRoute,
    ):
        """Find the open signal that this lifecycle event refers to.

        Strategy (cheap to expensive):
          1. If the lifecycle parser yields a trade_id, exact-match by it.
          2. Else extract a ticker from the caption and look up by symbol.
          3. Returns None if memory layer is disabled or no match.
        """
        if self._open_signals is None:
            return None

        content = message.content or ""

        # Strategy 1: trade_id (most reliable when available)
        trade_id: int | None = None
        try:
            if msg_type == MessageType.TP_HIT:
                trade_id = parse_tp_hit(content).trade_id
            elif msg_type == MessageType.ALL_TP_HIT:
                trade_id = parse_all_tp_hit(content).trade_id
            elif msg_type == MessageType.BREAKEVEN:
                trade_id = parse_breakeven(content).trade_id
            elif msg_type == MessageType.STOP_HIT:
                trade_id = parse_stop_hit(content).trade_id
            elif msg_type == MessageType.TRADE_CLOSED:
                trade_id = parse_trade_closed(content).trade_id
            elif msg_type == MessageType.CANCELED:
                trade_id = parse_canceled(content).trade_id
        except UpdateParseError:
            trade_id = None

        if trade_id is not None:
            try:
                hit = await self._open_signals.find_by_trade_id(
                    channel_id=message.channel_id, trade_id=trade_id,
                )
                if hit is not None:
                    return hit
            except Exception:
                logger.exception("open_signals.find_by_trade_id crashed")

        # Strategy 2: ticker from caption
        ticker = self._extract_pair_or_ticker(content)
        if not ticker:
            return None
        try:
            return await self._open_signals.find_latest_open(
                channel_id=message.channel_id, pair_or_base=ticker,
            )
        except Exception:
            logger.exception("open_signals.find_latest_open crashed")
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
