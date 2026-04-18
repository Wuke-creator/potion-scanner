"""Retention automations built on top of the signals bot.

Four features, one shared piece:

  - activity_db.py          shared: Discord post tracking (feeds 2 + 4)
  - activity_tracker.py     shared: on_message hook attached to the listener
  - feature_launch.py       Feature 1: /broadcast-feature DM fan-out
  - inactivity_detector.py  Feature 2: daily cron enrolls inactive users in
                            the re-engagement email sequence
  - value_reminder.py       Feature 3: monthly Telegram DM with personal stats
  - channel_feeler.py       Feature 4: channel-level feeler email when
                            engagement drops below threshold
"""

from src.automations.activity_db import ActivityDB

__all__ = ["ActivityDB"]
