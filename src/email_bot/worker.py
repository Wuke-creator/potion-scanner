"""Background worker that delivers due scheduled sends.

Polls the email DB every ``poll_interval_sec`` (default 60s), picks up
any rows where status='pending' AND due_at<=now, renders the template
with fresh stats from analytics.db, calls Resend, and marks the row as
sent or failed.

Does NOT retry failed sends automatically. Failed rows stay in the DB
with ``status='failed'`` and an error message so an operator can triage
them via Discord slash command or direct SQL.
"""

from __future__ import annotations

import asyncio
import logging

from src.email_bot.db import EmailDB, Subscriber
from src.email_bot.events_db import EmailEventsDB
from src.email_bot.sender import ResendClient, SendResult
from src.email_bot.stats import gather_stats
from src.email_bot.templates import render

logger = logging.getLogger(__name__)


class EmailWorker:
    """Delivery loop for scheduled email sends."""

    def __init__(
        self,
        db: EmailDB,
        sender: ResendClient,
        analytics_db_path: str,
        poll_interval_sec: float = 60.0,
        max_per_cycle: int = 50,
        send_rate_per_sec: float = 1.5,
        events_db: EmailEventsDB | None = None,
    ):
        """``send_rate_per_sec`` throttles inter-send delays inside a cycle
        so we stay under Resend's per-second cap. Resend's transactional
        /emails endpoint hard-caps at 2/sec on every plan we observe, so
        defaulting to 1.5 leaves headroom against burst rounding. Bump
        this if Resend confirms a higher per-account allowance. The
        worker sleeps ``1/send_rate_per_sec`` between consecutive sends
        in the same cycle. Cycles themselves are still gated by
        poll_interval_sec.
        """
        self._db = db
        self._sender = sender
        self._analytics_db_path = analytics_db_path
        self._poll_interval = poll_interval_sec
        self._max_per_cycle = max_per_cycle
        self._send_interval = 1.0 / max(send_rate_per_sec, 0.1)
        self._events_db = events_db
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="email_worker")
        logger.info(
            "Email worker started (poll=%.0fs, max_per_cycle=%d)",
            self._poll_interval, self._max_per_cycle,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None
        logger.info("Email worker stopped")

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._cycle()
            except Exception:
                logger.exception("Email worker cycle crashed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_interval,
                )
                return  # stop requested
            except asyncio.TimeoutError:
                continue

    async def _cycle(self) -> None:
        """One pass: fetch due sends, render, deliver, mark."""
        due = await self._db.due_sends()
        if not due:
            return

        # Cap the batch so one slow cycle can't starve shutdowns
        batch = due[: self._max_per_cycle]
        logger.info("Email worker cycle: %d due send(s)", len(batch))

        # Gather stats ONCE per cycle, reuse for every send in this batch.
        # The delta from rendering at the exact send time is negligible.
        try:
            stats = await gather_stats(self._analytics_db_path)
        except Exception:
            logger.exception("Could not load analytics stats; skipping cycle")
            return

        for i, send in enumerate(batch):
            if self._stop_event.is_set():
                break
            # Throttle between sends to stay under Resend's per-second
            # cap (2/sec free, 10/sec Pro). Skip the sleep before the
            # very first send so single-send cycles don't add latency.
            if i > 0:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._send_interval,
                    )
                    return  # stop requested mid-cycle
                except asyncio.TimeoutError:
                    pass
            try:
                await self._deliver_one(send, stats)
            except Exception:
                logger.exception(
                    "Unexpected error delivering send id=%d", send.id,
                )
                try:
                    await self._db.mark_failed(send.id, "unexpected error")
                except Exception:
                    logger.exception("Also failed to mark failed")

    async def _deliver_one(self, send, stats) -> None:
        sub = await self._db.get_subscriber(send.email)
        if sub is None:
            logger.warning(
                "Send id=%d has no subscriber row for %s; marking failed",
                send.id, send.email,
            )
            await self._db.mark_failed(send.id, "no subscriber row")
            return

        # Honor unsubscribes — recipient clicked our footer link or hit
        # the Gmail one-click button. Skip the send and mark it failed
        # so it never re-queues. Cheap lookup (UNIQUE index on recipient).
        if self._events_db is not None:
            try:
                if await self._events_db.is_unsubscribed(sub.email):
                    logger.info(
                        "Skipped %s day %d to %s: opted out",
                        send.sequence, send.day, sub.email,
                    )
                    await self._db.mark_failed(send.id, "opted_out")
                    return
            except Exception:
                # Don't block sends if the unsub lookup chokes — log and
                # let the send proceed. Risk of one extra send to an
                # unsubscribed recipient is lower than risk of stalling
                # the entire pipeline.
                logger.exception(
                    "Opt-out check crashed for %s; sending anyway",
                    sub.email,
                )

        try:
            email = render(
                sequence=send.sequence,
                day=send.day,
                subscriber=sub,
                stats=stats,
            )
        except Exception as e:
            logger.exception("Template render failed for send id=%d", send.id)
            await self._db.mark_failed(send.id, f"render error: {e}")
            return

        result: SendResult = await self._sender.send(
            to=sub.email,
            subject=email.subject,
            html=email.html,
            text=email.text,
            from_name=email.from_name,
            unsub_source=f"{send.sequence}_day{send.day}",
        )
        if result.ok:
            await self._db.mark_sent(send.id, resend_id=result.resend_id)
            logger.info(
                "Sent %s day %d to %s (resend_id=%s)",
                send.sequence, send.day, sub.email, result.resend_id,
            )
        else:
            await self._db.mark_failed(send.id, result.error or "unknown")
            logger.warning(
                "Failed %s day %d to %s: %s",
                send.sequence, send.day, sub.email, result.error,
            )
