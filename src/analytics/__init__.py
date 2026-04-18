"""Analytics subsystem: track signal counts + PnL over rolling windows.

The Potion Perps Bot emits leveraged PnL percentages directly in its
lifecycle messages (``PROFIT: 84%`` on a TP hit, ``LOSS: -77%`` on a stop).
The upstream update_parser extracts these as ``profit_pct`` and
``loss_pct``, so we just record whatever the source posted. No independent
PnL math, no matching trades to prices ourselves.

Two tables, both in a dedicated ``data/analytics.db`` file so this
subsystem can be migrated or rebuilt without touching user verification:

  - ``trades``: one row per SIGNAL_ALERT we forwarded. Holds trade_id,
    channel, pair, side, entry, leverage, opened_at.
  - ``trade_events``: one row per lifecycle event (TP hit, SL hit, etc.)
    with the pnl_pct value from the source message.

Queries power the ``/stats`` command:
  - count of signals per timeframe (weekly, monthly)
  - biggest PnL trade per timeframe (max pnl_pct across TP hits)
"""

from src.analytics.db import AnalyticsDB, ChannelStats, StatsWindow, TopPnL

__all__ = ["AnalyticsDB", "ChannelStats", "StatsWindow", "TopPnL"]
