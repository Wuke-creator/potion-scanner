"""Feature 4: Channel-level Feeler Email.

Drive spec reference: 04_In_App_Notifications.docx -> Task 19 (Feature
Highlight).

Luke's revision: not per-user per-channel. Channel-wide. When a tracked
channel gets low engagement (fewer than N unique posters in the last X
days), fire a feeler email to all members with email on file pointing
them to the channel.

Daily cron:
  1. For each channel in `feeler_channel_variants`:
     a. Count distinct posters in last `feeler_window_days`
     b. If count < threshold AND no feeler sent in last `feeler_cooldown_days`:
        - Pick the variant copy (A: Telegram Bot, B: Tools, C: Concierge)
        - Send email to all active verified users with email on file
        - Mark feeler sent for this channel

Rate-limited to stay under Resend's per-second cap.
"""

from __future__ import annotations

import asyncio
import logging
import time
from html import escape as html_escape

from src.automations.activity_db import ActivityDB
from src.automations.whop_members_db import WhopMembersDB
from src.email_bot.sender import ResendClient
from src.verification.db import VerificationDB

logger = logging.getLogger(__name__)


# Drive Task 19 variants. Keyed by the variant label in config.yaml's
# feeler_channel_variants map.
_VARIANT_COPY = {
    "telegram_bot": {
        "subject": "Have you set up the Telegram Alert Bot yet?",
        "body_lead": (
            "Quick one: you haven't set up the Telegram Alert Bot yet. "
            "It sends trade alerts straight to your phone in real time, so "
            "you don't need to stay in Discord all day."
        ),
        "cta_label": "Set up the bot",
    },
    "tools": {
        "subject": "You haven't checked out #tools-we-use",
        "body_lead": (
            "Did you know about #tools-we-use? The team drops the exact "
            "tools they use to find setups, updated weekly. You haven't "
            "checked it out yet. Worth a look."
        ),
        "cta_label": "Open the channel",
    },
    "concierge": {
        "subject": "Your Concierge thread is ready",
        "body_lead": (
            "Your Concierge thread is your direct line to the Potion team "
            "for trading questions, tool setup, or strategy feedback. Use "
            "it whenever you need help."
        ),
        "cta_label": "Open your Concierge thread",
    },
}


def _render_html(variant_key: str, cta_url: str) -> tuple[str, str, str]:
    copy = _VARIANT_COPY.get(variant_key) or _VARIANT_COPY["tools"]
    subject = copy["subject"]
    text = (
        f"Hey,\n\n"
        f"{copy['body_lead']}\n\n"
        f"{copy['cta_label']}: {cta_url}\n\n"
        f"Potion Team\n"
    )
    html = (
        '<!doctype html><html><body style="font-family:system-ui,sans-serif;'
        'max-width:560px;margin:0 auto;padding:24px;color:#222;line-height:1.5;">'
        "<p>Hey,</p>"
        f"<p>{html_escape(copy['body_lead'])}</p>"
        f'<p style="margin:32px 0;"><a href="{html_escape(cta_url)}" '
        'style="background:#6b4fbb;color:white;padding:14px 28px;'
        'text-decoration:none;border-radius:8px;font-weight:bold;'
        'display:inline-block;">'
        f"{html_escape(copy['cta_label'])}</a></p>"
        "<p>Potion Team</p>"
        "</body></html>"
    )
    return subject, text, html


class ChannelFeeler:
    """Background cron that fires feeler emails when channel engagement drops."""

    def __init__(
        self,
        activity_db: ActivityDB,
        resend_client: ResendClient,
        variant_by_channel: dict[int, str],
        low_engagement_threshold: int = 5,
        window_days: int = 14,
        cooldown_days: int = 30,
        interval_hours: int = 24,
        send_rate_per_sec: float = 5.0,
        cta_url_by_variant: dict[str, str] | None = None,
        whop_members_db: WhopMembersDB | None = None,
        verification_db: VerificationDB | None = None,
    ):
        """whop_members_db is the preferred recipient source (full Elite
        roster). verification_db is a fallback for the Telegram-verified
        subset when the Whop sync hasn't run yet."""
        self._activity_db = activity_db
        self._verification_db = verification_db
        self._whop_members_db = whop_members_db
        self._resend = resend_client
        self._variants = dict(variant_by_channel)
        self._threshold = low_engagement_threshold
        self._window_sec = window_days * 86400
        self._cooldown_sec = cooldown_days * 86400
        self._interval_sec = interval_hours * 3600
        self._send_interval = 1.0 / send_rate_per_sec
        self._cta_urls = cta_url_by_variant or {}
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="channel_feeler")
        logger.info(
            "ChannelFeeler started (threshold=%d, window=%dd, cooldown=%dd, channels=%d)",
            self._threshold, self._window_sec // 86400,
            self._cooldown_sec // 86400, len(self._variants),
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
        logger.info("ChannelFeeler stopped")

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("ChannelFeeler cycle crashed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_sec,
                )
                return
            except asyncio.TimeoutError:
                continue

    async def run_once(self) -> dict:
        """One cycle. Returns summary stats."""
        now = int(time.time())
        cutoff = now - self._window_sec
        summary = {"channels_scanned": 0, "feelers_fired": 0, "emails_sent": 0, "emails_failed": 0}

        for channel_id, variant_key in self._variants.items():
            summary["channels_scanned"] += 1
            posters = await self._activity_db.count_unique_posters(channel_id, cutoff)
            if posters >= self._threshold:
                continue
            if not await self._activity_db.can_send_feeler(channel_id, self._cooldown_sec):
                continue

            # Fire feeler for this channel. Prefer whop_members (full Elite
            # roster) so non-Telegram users get the feeler too; fall back to
            # verified_users if Whop sync hasn't populated anything yet.
            recipient_emails: list[str] = []
            if self._whop_members_db is not None:
                rows = await self._whop_members_db.list_valid_with_email()
                recipient_emails = [r.email for r in rows]
            if not recipient_emails and self._verification_db is not None:
                verified = await self._verification_db.list_active_with_email()
                recipient_emails = [u.email for u in verified]
            # De-dup + drop blanks just in case.
            recipient_emails = list({e for e in recipient_emails if e})
            if not recipient_emails:
                logger.info(
                    "ChannelFeeler skipped channel %d: no recipients with email",
                    channel_id,
                )
                continue

            subject, text, html = _render_html(
                variant_key,
                cta_url=self._cta_urls.get(variant_key, "https://whop.com/potion"),
            )
            fired_for_channel = 0
            for email in recipient_emails:
                try:
                    result = await self._resend.send(
                        to=email, subject=subject, html=html, text=text,
                        from_name="Potion Alpha Team",
                    )
                    if result.ok:
                        summary["emails_sent"] += 1
                        fired_for_channel += 1
                    else:
                        summary["emails_failed"] += 1
                        logger.warning(
                            "Feeler email failed to %s: %s", email, result.error,
                        )
                except Exception:
                    summary["emails_failed"] += 1
                    logger.exception("Feeler send crashed for %s", email)
                await asyncio.sleep(self._send_interval)

            await self._activity_db.mark_feeler_sent(channel_id, when=now)
            summary["feelers_fired"] += 1
            logger.info(
                "ChannelFeeler fired for channel %d (variant=%s, posters=%d<%d), %d emails sent",
                channel_id, variant_key, posters, self._threshold, fired_for_channel,
            )

        logger.info("ChannelFeeler cycle: %s", summary)
        return summary
