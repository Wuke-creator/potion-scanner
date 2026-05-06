"""Ops capture subsystem.

Listens to a configured set of "ops" channels (general chat, alpha chat,
plus a forum channel whose threads are tickets) and persists every
non-bot message to data/ops.db. Also detects @mentions of senior staff
and persists those into a separate leadership_mentions table for the
dashboard's leadership panel.

This module is read-only towards the existing bot subsystems. It hooks
into the existing DiscordListener via an ``ops_hook`` callback, so it
shares the same gateway connection and intent set. No new privileged
intents are required beyond what the signal pipeline already uses.
"""

from src.ops.db import OpsDB
from src.ops.capture import OpsCapture

__all__ = ["OpsDB", "OpsCapture"]
