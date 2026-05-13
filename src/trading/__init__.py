"""Trading subsystem: Ostium builder integration.

Users register a delegate key once via app.ostium.com/sdk-export, paste it
into the bot via /connect, and tap 1-Tap Trade on signal alerts to fire
trades against Ostium without further wallet popups.

Submodules:

  delegates_db        Encrypted store of per-user delegate keys + trader address
  user_settings_db    Per-user slippage tolerance + size-preset list
  disclosure          Risk-disclosure copy shown during /connect
  executor_client     HTTP client for the Node.js trade-executor sidecar
  commands            /connect, /disconnect, /trading Telegram handlers
  trade_flow          1-Tap Trade callback handlers (confirmation card,
                      size selection, submit)
"""

from src.trading.delegates_db import DelegatesDB, TradingDelegate
from src.trading.user_settings_db import UserTradingSettingsDB, UserTradingSettings

__all__ = [
    "DelegatesDB",
    "TradingDelegate",
    "UserTradingSettingsDB",
    "UserTradingSettings",
]
