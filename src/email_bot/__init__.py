"""Email bot: win-back + re-engagement sequences for Potion Alpha members.

Triggered by Whop cancellation webhooks (4-email win-back sequence) or by
a Discord inactivity detector (4-email re-engagement sequence). Pulls
dynamic stats (call counts, top PnL) from the shared analytics DB that
the Potion Scanner TG bot populates.

Composed of:

  - db.py         SQLite store for subscribers + scheduled sends
  - stats.py      Read-only queries against analytics.db for template copy
  - templates.py  The 8 email templates + 6 offer variants
  - sender.py     Resend HTTP client
  - worker.py     Background async loop that delivers due sends
  - webhook.py    aiohttp route handlers for Whop + admin triggers
  - runtime.py    Lifecycle glue for main.py
"""

from src.email_bot.db import EmailDB, ScheduledSend, Subscriber

__all__ = ["EmailDB", "ScheduledSend", "Subscriber"]
