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
import dataclasses
import logging

from src.email_bot.db import EmailDB, Subscriber
from src.email_bot.sender import ResendClient, SendResult
from src.email_bot.stats import gather_stats
from src.email_bot.templates import render
from src.whop_api import create_one_time_promo

logger = logging.getLogger(__name__)

# Bronze day-5 carries the only discount in the sequence. Minted at
# send time (not enrollment) so the recipient gets a full redemption
# window from the offer email, and so the day-1/day-3 plain links never
# leak the discount before it's "revealed".
_BRONZE_PROMO_BASE_CODE = "BRONZE30"
_BRONZE_PROMO_AMOUNT_OFF = 30.0
_BRONZE_PROMO_DURATION_MONTHS = 1   # discount applies to the first Elite cycle


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
        whop_promo_api_key: str = "",
        whop_company_id: str = "",
        bronze_promo_ttl_days: int = 14,
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
        # Bronze day-5 promo minting. When the keys are unset the day-5
        # email still sends, just with the plain link (no discount
        # pre-applied) — never block the send on the promo API.
        self._whop_promo_api_key = whop_promo_api_key
        self._whop_company_id = whop_company_id
        self._bronze_promo_ttl_days = bronze_promo_ttl_days
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

    async def _bronze_day5_subscriber(self, sub: Subscriber) -> Subscriber:
        """Return a per-send copy of ``sub`` whose rejoin_url carries a
        freshly-minted single-use 30%-off Elite promo.

        The code is single-use (``create_one_time_promo`` sets stock=1 +
        one_per_customer) and expires after ``bronze_promo_ttl_days``
        (default 14). Minting at send time means the recipient gets the
        full window from the moment the offer email lands.

        On any failure (keys unset, Whop rejects, network) we log and
        return the original subscriber unchanged so the email still goes
        out — the link works, it just won't have the discount
        pre-applied. We never drop the send over a promo problem.
        """
        if not (self._whop_promo_api_key and self._whop_company_id):
            logger.warning(
                "Bronze day5 for %s: promo API not configured — sending "
                "plain link (no discount pre-applied)",
                sub.email,
            )
            return sub
        try:
            code = await create_one_time_promo(
                api_key=self._whop_promo_api_key,
                company_id=self._whop_company_id,
                base_code=_BRONZE_PROMO_BASE_CODE,
                amount_off=_BRONZE_PROMO_AMOUNT_OFF,
                duration_months=_BRONZE_PROMO_DURATION_MONTHS,
                reason_tag="bronze_upsell",
                ttl_days=self._bronze_promo_ttl_days,
            )
        except Exception:
            logger.exception(
                "Bronze day5 promo mint crashed for %s", sub.email,
            )
            code = None
        if not code:
            logger.warning(
                "Bronze day5 for %s: promo mint failed/disabled — sending "
                "plain link (no discount pre-applied)",
                sub.email,
            )
            return sub
        base = sub.rejoin_url or "https://whop.com/potion"
        sep = "&" if "?" in base else "?"
        promo_url = f"{base}{sep}promo={code}"
        logger.info(
            "Bronze day5 for %s: minted single-use promo %s (ttl=%dd)",
            sub.email, code, self._bronze_promo_ttl_days,
        )
        # Per-send override only — the subscriber row stays on the plain
        # URL so a re-send (rare) re-mints a fresh single-use code rather
        # than reusing a spent one.
        return dataclasses.replace(sub, rejoin_url=promo_url)

    async def _deliver_one(self, send, stats) -> None:
        sub = await self._db.get_subscriber(send.email)
        if sub is None:
            logger.warning(
                "Send id=%d has no subscriber row for %s; marking failed",
                send.id, send.email,
            )
            await self._db.mark_failed(send.id, "no subscriber row")
            return

        # Bronze day 5 is the only email in the sequence that reveals a
        # discount. Mint a fresh single-use code now (send time) and
        # render with a per-send copy of the subscriber carrying the
        # promo'd URL. Days 1/3 fall through untouched (plain link).
        render_sub = sub
        if send.sequence == "bronze" and send.day == 5:
            render_sub = await self._bronze_day5_subscriber(sub)

        try:
            email = render(
                sequence=send.sequence,
                day=send.day,
                subscriber=render_sub,
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
