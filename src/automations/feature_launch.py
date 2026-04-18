"""Feature 1: Feature Launch Blast.

Drive spec reference: 04_In_App_Notifications.docx → Task 20 (Feature launch announcement).

Staff-triggered fan-out to all active verified users announcing a new
product feature. Telegram DM to everyone (instant), email to members
with email on file (richer format).

Admin invokes via `/broadcast-feature` slash command on the bot.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from html import escape as html_escape

from telegram import Bot
from telegram.error import Forbidden, RetryAfter, TelegramError

from src.automations.whop_members_db import WhopMembersDB
from src.email_bot.sender import ResendClient
from src.verification.db import VerificationDB

logger = logging.getLogger(__name__)


@dataclass
class LaunchStats:
    dm_attempted: int = 0
    dm_sent: int = 0
    dm_blocked: int = 0
    dm_failed: int = 0
    email_attempted: int = 0
    email_sent: int = 0
    email_failed: int = 0
    duration_sec: float = 0.0


def _build_dm_text(title: str, description: str, cta_url: str) -> str:
    """Markdown-safe Telegram DM."""
    return (
        f"\U0001f6a2 *Something new just dropped in Potion*\n\n"
        f"*{title}*\n\n"
        f"{description}\n\n"
        f"If you've been waiting for the right time to come back, this might be it.\n\n"
        f"{cta_url}"
    )


def _build_email_html(title: str, description: str, cta_url: str, name: str = "") -> str:
    greeting = f"Hey {html_escape(name)}," if name else "Hey,"
    return (
        '<!doctype html><html><body style="font-family:system-ui,sans-serif;'
        'max-width:560px;margin:0 auto;padding:24px;color:#222;line-height:1.5;">'
        f"<p>{greeting}</p>"
        "<p>We've been building. Here's what just shipped:</p>"
        f"<h2 style='margin:24px 0 8px 0;'>\U0001f195 {html_escape(title)}</h2>"
        f"<p>{html_escape(description)}</p>"
        "<p>This is exactly the kind of thing we heard members asking for.</p>"
        f'<p style="margin:32px 0;"><a href="{html_escape(cta_url)}" '
        'style="background:#6b4fbb;color:white;padding:14px 28px;'
        'text-decoration:none;border-radius:8px;font-weight:bold;'
        'display:inline-block;">Check it out</a></p>'
        "</body></html>"
    )


def _build_email_text(title: str, description: str, cta_url: str, name: str = "") -> str:
    greeting = f"Hey {name}," if name else "Hey,"
    return (
        f"{greeting}\n\n"
        "We've been building. Here's what just shipped:\n\n"
        f"{title}\n"
        f"{description}\n\n"
        "This is exactly the kind of thing we heard members asking for.\n\n"
        f"Check it out: {cta_url}\n"
    )


class FeatureLaunchBroadcaster:
    """Fan-out helper for /broadcast-feature slash command.

    Reuses the Telegram bot directly (not the signal Dispatcher) because
    signal Dispatcher filters by channel subscriptions, which don't apply
    here. This is a universal broadcast to every active verified user.

    Rate-limits itself to ~20 Telegram sends/sec to stay well under the
    30/sec global Telegram limit, and ~5 Resend sends/sec to respect
    Resend's free-tier limit (higher on Pro).
    """

    def __init__(
        self,
        telegram_bot: Bot,
        verification_db: VerificationDB,
        resend_client: ResendClient | None = None,
        cta_url: str = "https://whop.com/potion",
        telegram_rate_per_sec: float = 20.0,
        email_rate_per_sec: float = 5.0,
        whop_members_db: WhopMembersDB | None = None,
    ):
        """DMs use verification_db (Telegram-verified subset). Emails prefer
        whop_members_db (full Elite roster) so we reach non-Telegram members
        too, falling back to verification_db.list_active() + email filter if
        whop_members isn't populated yet."""
        self._bot = telegram_bot
        self._db = verification_db
        self._whop_members_db = whop_members_db
        self._resend = resend_client
        self._cta_url = cta_url
        self._tg_interval = 1.0 / telegram_rate_per_sec
        self._email_interval = 1.0 / email_rate_per_sec

    async def broadcast(
        self, title: str, description: str, include_email: bool = True,
    ) -> LaunchStats:
        """Fire DMs to every active verified user + optional email half."""
        start = time.monotonic()
        stats = LaunchStats()

        users = await self._db.list_active()
        dm_text = _build_dm_text(title, description, self._cta_url)

        # Telegram DMs
        for user in users:
            stats.dm_attempted += 1
            try:
                await self._bot.send_message(
                    chat_id=user.telegram_user_id,
                    text=dm_text,
                    parse_mode="Markdown",
                    disable_web_page_preview=False,
                )
                stats.dm_sent += 1
            except Forbidden:
                stats.dm_blocked += 1
                logger.info(
                    "User %d has blocked the bot; skipping",
                    user.telegram_user_id,
                )
            except RetryAfter as e:
                stats.dm_failed += 1
                logger.warning(
                    "Telegram RetryAfter on launch blast: sleeping %.1fs",
                    float(e.retry_after),
                )
                await asyncio.sleep(min(float(e.retry_after), 30.0))
            except TelegramError:
                stats.dm_failed += 1
                logger.exception(
                    "DM failed for user %d", user.telegram_user_id,
                )
            except Exception:
                stats.dm_failed += 1
                logger.exception(
                    "Unexpected DM error for user %d", user.telegram_user_id,
                )
            await asyncio.sleep(self._tg_interval)

        # Email half. Prefer whop_members (full Elite roster) so non-Telegram
        # members get the blast too. Fall back to the Telegram-verified
        # subset's emails if Whop sync hasn't run yet.
        if include_email and self._resend is not None:
            emails: list[str] = []
            if self._whop_members_db is not None:
                rows = await self._whop_members_db.list_valid_with_email()
                emails = [r.email for r in rows]
            if not emails:
                emails = [u.email for u in users if u.email]
            # De-dup in case the same email appears in both sources.
            emails = list({e for e in emails if e})
            subject = f"Something new just dropped in Potion: {title}"
            for email in emails:
                stats.email_attempted += 1
                try:
                    html = _build_email_html(title, description, self._cta_url)
                    text = _build_email_text(title, description, self._cta_url)
                    result = await self._resend.send(
                        to=email,
                        subject=subject,
                        html=html,
                        text=text,
                        from_name="Potion Alpha Team",
                    )
                    if result.ok:
                        stats.email_sent += 1
                    else:
                        stats.email_failed += 1
                        logger.warning(
                            "Email failed to %s: %s", email, result.error,
                        )
                except Exception:
                    stats.email_failed += 1
                    logger.exception("Email send crashed for %s", email)
                await asyncio.sleep(self._email_interval)

        stats.duration_sec = time.monotonic() - start
        logger.info(
            "Feature launch complete: DM %d/%d, email %d/%d, %.1fs",
            stats.dm_sent, stats.dm_attempted,
            stats.email_sent, stats.email_attempted,
            stats.duration_sec,
        )
        return stats
