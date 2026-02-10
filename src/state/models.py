"""Data models for trade and order state persistence."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class TradeStatus(Enum):
    """Lifecycle status of a trade."""

    PREPARING = "preparing"  # Preparation message received, not yet actionable
    PENDING = "pending"      # Signal received, entry order not yet placed
    OPEN = "open"            # Entry filled, position is live
    CLOSED = "closed"        # All TPs hit, SL hit, or manually closed
    CANCELED = "canceled"    # Trade canceled before or after entry


class OrderType(Enum):
    """Which part of the trade this order represents."""

    ENTRY = "entry"
    STOP_LOSS = "stop_loss"
    TP1 = "tp1"
    TP2 = "tp2"
    TP3 = "tp3"


class OrderStatus(Enum):
    """Lifecycle status of an individual order on the exchange."""

    PENDING = "pending"      # Built but not yet submitted
    SUBMITTED = "submitted"  # Sent to exchange, resting
    FILLED = "filled"        # Fully filled
    CANCELED = "canceled"    # Canceled (by us or exchange)
    REJECTED = "rejected"    # Exchange rejected the order


@dataclass
class TradeRecord:
    """One row per signal — tracks the full lifecycle of a trade."""

    trade_id: int            # Potion Perps trade ID (e.g. 1286)
    user_id: str
    pair: str                # e.g. "ZK/USDT"
    coin: str                # Hyperliquid coin name (e.g. "ZK")
    side: str                # "LONG" or "SHORT"
    risk_level: str          # "LOW", "MEDIUM", "HIGH"
    trade_type: str          # "SWING" or "SCALP"
    size_hint: str           # e.g. "1-4%"
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    leverage: int            # Actual leverage used (after capping)
    signal_leverage: int     # Original leverage from signal
    position_size_usd: float # Actual USD position size
    position_size_coin: float  # Actual coin quantity
    status: TradeStatus = TradeStatus.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    closed_at: datetime | None = None
    close_reason: str | None = None  # "all_tp_hit", "stop_hit", "manual", "canceled"
    pnl_pct: float | None = None     # Final P&L % (from signal provider or calculated)


@dataclass
class OrderRecord:
    """One row per order placed on the exchange."""

    id: int | None           # Auto-increment primary key
    trade_id: int            # FK to TradeRecord.trade_id
    user_id: str
    order_type: OrderType    # entry, stop_loss, tp1, tp2, tp3
    coin: str
    side: str                # "BUY" or "SELL"
    size: float              # Order quantity
    price: float             # Limit / trigger price
    oid: int | None = None   # Hyperliquid order ID (set after submission)
    status: OrderStatus = OrderStatus.PENDING
    fill_price: float | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
