"""Pre-send gate that wraps ``ResendClient``.

Every retention email goes through this wrapper before reaching Resend.
Two gates run in front of every send:

  1. Suppression check: refuses to send to recipients with a recorded
     hard bounce, complaint, repeated soft bounces, or List-Unsubscribe
     click. Delegated to ``EmailEventsDB.is_suppressed_recipient``.

  2. Per-recipient throttle: refuses to send if the same address received
     a successful send inside ``throttle_window_sec`` (default 30 minutes).
     Reads from ``EmailDB.last_sent_at``.

When either gate trips, the wrapper returns a ``SendResult(ok=False,
error=...)`` so the worker can decide what to do (mark the scheduled row
canceled, increment the suppression-rate metric, log, move on). The
inner client is never invoked on a blocked send, so no Resend quota is
spent.

Construct once at startup with the shared ``EmailEventsDB`` /
``EmailDB`` handles; pass the wrapper to ``EmailWorker`` in place of the
raw ``ResendClient``.
"""

from __future__ import annotations

import logging
import time

from src.email_bot.db import EmailDB
from src.email_bot.events_db import EmailEventsDB
from src.email_bot.sender import ResendClient, SendResult

logger = logging.getLogger(__name__)


DEFAULT_THROTTLE_WINDOW_SEC = 30 * 60


class ProtectedSender:
    """Suppression + throttle gate in front of ``ResendClient.send``."""

    def __init__(
        self,
        client: ResendClient,
        events_db: EmailEventsDB,
        email_db: EmailDB,
        throttle_window_sec: int = DEFAULT_THROTTLE_WINDOW_SEC,
    ):
        self._client = client
        self._events_db = events_db
        self._email_db = email_db
        self._throttle_window_sec = max(0, int(throttle_window_sec))

    async def send(
        self,
        to: str,
        subject: str,
        html: str,
        text: str,
        unsubscribe_url: str | None = None,
        from_name: str | None = None,
        reply_to: str | None = None,
    ) -> SendResult:
        """Gate the send. Returns the inner client's result on success,
        or a synthetic refusal result when a gate trips."""
        if not to:
            return SendResult(ok=False, error="missing recipient")

        try:
            suppressed, reason = await self._events_db.is_suppressed_recipient(
                to,
            )
        except Exception as e:
            logger.exception(
                "ProtectedSender: suppression check crashed for %s; "
                "failing closed",
                to,
            )
            return SendResult(
                ok=False, error=f"suppression check error: {e}",
            )
        if suppressed:
            logger.info(
                "ProtectedSender: refusing send to %s (suppressed: %s)",
                to, reason,
            )
            return SendResult(ok=False, error=f"suppressed: {reason}")

        if self._throttle_window_sec > 0:
            try:
                last = await self._email_db.last_sent_at(to)
            except Exception as e:
                logger.exception(
                    "ProtectedSender: last_sent_at crashed for %s; "
                    "failing closed",
                    to,
                )
                return SendResult(
                    ok=False, error=f"throttle check error: {e}",
                )
            if last is not None:
                age = int(time.time()) - int(last)
                if 0 <= age < self._throttle_window_sec:
                    logger.warning(
                        "ProtectedSender: throttling send to %s "
                        "(last send was %ds ago, window=%ds)",
                        to, age, self._throttle_window_sec,
                    )
                    return SendResult(
                        ok=False,
                        error=(
                            f"throttled: last send was {age} seconds ago"
                        ),
                    )

        return await self._client.send(
            to=to,
            subject=subject,
            html=html,
            text=text,
            from_name=from_name,
            reply_to=reply_to,
            unsubscribe_url=unsubscribe_url,
        )
