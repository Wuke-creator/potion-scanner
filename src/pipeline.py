"""Signal processing pipeline — the core orchestrator.

Receives raw messages, classifies them, and dispatches to the appropriate
handler. For new signals: sizes the position, builds orders, submits to
the exchange, and records in the database. For lifecycle events: updates
the trade state accordingly.
"""

import logging

from src.config.settings import Config, StrategyPreset
from src.exchange.hyperliquid import HyperliquidClient
from src.exchange.order_builder import TradeOrderSet, build_orders
from src.exchange.position_manager import PositionManager
from src.parser.classifier import MessageType, classify
from src.parser.signal_parser import ParsedSignal, SignalParseError, parse_signal
from src.parser.update_parser import (
    UpdateParseError,
    parse_all_tp_hit,
    parse_breakeven,
    parse_canceled,
    parse_manual_update,
    parse_preparation,
    parse_sl_update,
    parse_stop_hit,
    parse_tp_hit,
    parse_trade_closed,
)
from src.state.database import TradeDatabase
from src.state.models import TradeRecord, TradeStatus
from src.strategy.position_sizer import (
    PositionSizeError,
    RiskLimitBreached,
    calculate_position_size,
    check_risk_limits,
)

logger = logging.getLogger(__name__)


class Pipeline:
    """Processes raw signal messages end-to-end.

    Wires together: classifier → parser → position sizer → order builder
    → position manager → database.

    Each instance is scoped to a single user.
    """

    def __init__(
        self,
        config: Config,
        client: HyperliquidClient,
        db: TradeDatabase,
    ):
        self._config = config
        self._client = client
        self._db = db
        self._pm = PositionManager(client, db)
        self._asset_meta = client.get_asset_meta()

    def process_message(self, raw_message: str) -> None:
        """Classify and process a single raw message.

        This is the main entry point — call once per incoming message.
        """
        msg_type = classify(raw_message)
        logger.info("Classified message as: %s", msg_type.value)

        handlers = {
            MessageType.SIGNAL_ALERT: self._handle_signal,
            MessageType.TP_HIT: self._handle_tp_hit,
            MessageType.ALL_TP_HIT: self._handle_all_tp_hit,
            MessageType.BREAKEVEN: self._handle_breakeven,
            MessageType.STOP_HIT: self._handle_stop_hit,
            MessageType.CANCELED: self._handle_canceled,
            MessageType.TRADE_CLOSED: self._handle_trade_closed,
            MessageType.PREPARATION: self._handle_preparation,
            MessageType.MANUAL_UPDATE: self._handle_manual_update,
            MessageType.NOISE: self._handle_noise,
        }

        handler = handlers.get(msg_type, self._handle_noise)
        try:
            handler(raw_message)
        except (SignalParseError, UpdateParseError) as e:
            logger.error("Parse error for %s: %s", msg_type.value, e)
        except Exception as e:
            logger.error("Unexpected error handling %s: %s", msg_type.value, e, exc_info=True)

    # ------------------------------------------------------------------
    # Signal handling — new trade
    # ------------------------------------------------------------------

    def _handle_signal(self, raw: str) -> None:
        """Parse signal → size → build orders → submit."""
        signal = parse_signal(raw)
        preset = self._config.get_active_preset()

        # Check if we already have this trade
        existing = self._db.get_trade(signal.trade_id)
        if existing and existing.status in (TradeStatus.OPEN, TradeStatus.PENDING):
            logger.warning("Trade #%d already exists (status=%s), skipping", signal.trade_id, existing.status.value)
            return

        # Calculate position size
        balance = self._get_balance_usd()
        try:
            position_size_usd = calculate_position_size(
                balance, signal.risk_level.value, preset,
                self._config.strategy, self._config.risk,
            )
        except PositionSizeError as e:
            logger.warning("Skipping trade #%d: %s", signal.trade_id, e)
            return

        # Risk gate — check all limits before proceeding
        try:
            check_risk_limits(
                risk_config=self._config.risk,
                open_trade_count=len(self._db.get_open_trades()),
                daily_pnl_pct=self._db.get_daily_closed_pnl(),
                total_exposure_usd=self._db.get_total_open_exposure_usd(),
                new_position_usd=position_size_usd,
            )
        except RiskLimitBreached as e:
            logger.warning("Skipping trade #%d — risk limit: %s", signal.trade_id, e)
            return

        # Build orders
        trade_set = build_orders(
            signal, position_size_usd, self._asset_meta,
            tp_split=preset.tp_split,
            max_leverage=self._config.strategy.max_leverage,
        )

        # Record trade in DB
        trade_record = TradeRecord(
            trade_id=signal.trade_id,
            user_id=self._db.user_id,
            pair=signal.pair,
            coin=trade_set.coin,
            side=signal.side.value,
            risk_level=signal.risk_level.value,
            trade_type=signal.trade_type,
            size_hint=signal.size,
            entry_price=signal.entry,
            stop_loss=signal.stop_loss,
            tp1=signal.tp1,
            tp2=signal.tp2,
            tp3=signal.tp3,
            leverage=trade_set.leverage,
            signal_leverage=signal.leverage,
            position_size_usd=position_size_usd,
            position_size_coin=trade_set.entry.sz,
        )
        self._db.create_trade(trade_record)

        # Submit to exchange
        if self._config.strategy.auto_execute:
            success = self._pm.submit_trade(trade_set)
            if success:
                logger.info(
                    "Trade #%d submitted: %s %s %s @ %s (lev=%dx, size=$%.2f)",
                    signal.trade_id, trade_set.coin, signal.side.value,
                    trade_set.entry.sz, signal.entry, trade_set.leverage, position_size_usd,
                )
            else:
                logger.error("Trade #%d submission failed", signal.trade_id)
                self._db.update_trade_status(signal.trade_id, TradeStatus.CANCELED, close_reason="submission_failed")
        else:
            logger.info(
                "Trade #%d ready (auto_execute=false): %s %s %s @ %s (lev=%dx, size=$%.2f)",
                signal.trade_id, trade_set.coin, signal.side.value,
                trade_set.entry.sz, signal.entry, trade_set.leverage, position_size_usd,
            )

    # ------------------------------------------------------------------
    # Lifecycle events
    # ------------------------------------------------------------------

    def _handle_tp_hit(self, raw: str) -> None:
        """TP hit — update DB, optionally move SL to breakeven."""
        tp = parse_tp_hit(raw)
        trade = self._db.get_trade(tp.trade_id)
        if not trade:
            logger.warning("TP hit for unknown trade #%d", tp.trade_id)
            return

        logger.info("TP%d hit for trade #%d %s (+%.2f%%)", tp.tp_number, tp.trade_id, tp.pair, tp.profit_pct)

        # Check if we should move SL to breakeven
        preset = self._config.get_active_preset()
        be_after = preset.move_sl_to_breakeven_after
        should_move = (
            (be_after == "tp1" and tp.tp_number == 1) or
            (be_after == "tp2" and tp.tp_number == 2)
        )
        if should_move and trade.status == TradeStatus.OPEN:
            self._pm.move_sl_to_breakeven(tp.trade_id, trade.coin, trade.entry_price)

    def _handle_all_tp_hit(self, raw: str) -> None:
        """All TPs hit — trade is fully closed."""
        atp = parse_all_tp_hit(raw)
        trade = self._db.get_trade(atp.trade_id)
        if not trade:
            logger.warning("All TP hit for unknown trade #%d", atp.trade_id)
            return

        logger.info("All TPs hit for trade #%d %s (+%.2f%%)", atp.trade_id, atp.pair, atp.profit_pct)
        self._db.update_trade_status(
            atp.trade_id, TradeStatus.CLOSED,
            close_reason="all_tp_hit", pnl_pct=atp.profit_pct,
        )

    def _handle_breakeven(self, raw: str) -> None:
        """Breakeven — move SL to entry if not already done by TP-hit auto-move."""
        be = parse_breakeven(raw)
        trade = self._db.get_trade(be.trade_id)
        if not trade:
            logger.warning("Breakeven for unknown trade #%d", be.trade_id)
            return

        logger.info("Breakeven for trade #%d %s (TP%d secured)", be.trade_id, be.pair, be.tp_secured)

        if trade.status == TradeStatus.OPEN and self._config.strategy.auto_execute:
            moved = self._pm.move_sl_to_breakeven(be.trade_id, trade.coin, trade.entry_price)
            if moved:
                logger.info("SL moved to breakeven for trade #%d via provider message", be.trade_id)
            # If move_sl_to_breakeven returns False it means no active SL was found
            # (likely already moved by the TP-hit auto-move) — that's fine.

    def _handle_stop_hit(self, raw: str) -> None:
        """Stop hit — trade is closed at a loss."""
        sh = parse_stop_hit(raw)
        trade = self._db.get_trade(sh.trade_id)
        if not trade:
            logger.warning("Stop hit for unknown trade #%d", sh.trade_id)
            return

        logger.info("Stop hit for trade #%d %s (%.2f%%)", sh.trade_id, sh.pair, sh.loss_pct)
        self._db.update_trade_status(
            sh.trade_id, TradeStatus.CLOSED,
            close_reason="stop_hit", pnl_pct=sh.loss_pct,
        )

    def _handle_canceled(self, raw: str) -> None:
        """Trade canceled — cancel all orders on exchange."""
        cancel = parse_canceled(raw)
        trade = self._db.get_trade(cancel.trade_id)
        if not trade:
            logger.warning("Cancel for unknown trade #%d", cancel.trade_id)
            return

        logger.info("Trade #%d canceled: %s", cancel.trade_id, cancel.reason)
        if trade.status == TradeStatus.OPEN:
            self._pm.close_position(cancel.trade_id, trade.coin, reason="canceled")
        else:
            self._pm.cancel_trade(cancel.trade_id)

    def _handle_trade_closed(self, raw: str) -> None:
        """Trade manually closed — close position on exchange."""
        tc = parse_trade_closed(raw)
        trade = self._db.get_trade(tc.trade_id)
        if not trade:
            logger.warning("Trade closed for unknown trade #%d", tc.trade_id)
            return

        logger.info("Trade #%d closed: %s", tc.trade_id, tc.detail)
        if trade.status == TradeStatus.OPEN:
            self._pm.close_position(tc.trade_id, trade.coin, reason="manual_close")
        else:
            self._db.update_trade_status(tc.trade_id, TradeStatus.CLOSED, close_reason="manual_close")

    def _handle_preparation(self, raw: str) -> None:
        """Preparation message — log but do not execute."""
        prep = parse_preparation(raw)
        logger.info(
            "Preparation: trade #%d %s %s (entry=%s, lev=%s) — NOT executing",
            prep.trade_id, prep.pair, prep.side, prep.entry, prep.leverage,
        )

    def _handle_manual_update(self, raw: str) -> None:
        """Manual update — try to detect actionable instructions (e.g. SL moves)."""
        # Try to detect an SL adjustment instruction first
        sl = parse_sl_update(raw)
        if sl:
            trade = self._db.get_trade(sl.trade_id)
            if not trade:
                logger.warning("SL update for unknown trade #%d", sl.trade_id)
                return
            if trade.status != TradeStatus.OPEN:
                logger.warning("SL update for non-open trade #%d (status=%s)", sl.trade_id, trade.status.value)
                return

            logger.info("SL update detected: trade #%d → new SL %.6f", sl.trade_id, sl.new_price)
            if self._config.strategy.auto_execute:
                self._pm.move_stop_loss(sl.trade_id, trade.coin, sl.new_price)
            else:
                logger.info("SL update ready (auto_execute=false) for trade #%d", sl.trade_id)
            return

        # Not an SL instruction — log for human review
        mu = parse_manual_update(raw)
        logger.info(
            "Manual update: trade #%s %s — %s",
            mu.trade_id, mu.pair, mu.instruction,
        )

    def _handle_noise(self, raw: str) -> None:
        """Noise — ignore."""
        logger.debug("Noise message ignored")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_balance_usd(self) -> float:
        """Get current USDC balance."""
        bal = self._client.get_balance()
        return float(bal["usdc_balance"])
