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
    ):
        self._db = db
        self._sender = sender
        self._analytics_db_path = analytics_db_path
        self._poll_interval = poll_interval_sec
        self._max_per_cycle = max_per_cycle
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

        for send in batch:
            if self._stop_event.is_set():
                break
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
        )
        if result.ok:
            await self._db.mark_sent(send.id)
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
