"""Cross-DB analytics for the email subsystem.

The email subsystem stores its data in three separate SQLite files:

  data/email.db          subscribers + scheduled_sends (managed by EmailDB)
  data/email_events.db   raw Resend webhook events    (managed by EmailEventsDB)
  data/verification.db   Telegram-verified Whop members (managed by VerifiedDB)

This module joins those stores in Python rather than via SQLite ATTACH so
each DB keeps its own schema, lock domain, and lifecycle. ATTACH-ing across
three files would also force every analytics query to take a write lock on
all three connections at once — overkill for the read traffic we expect.

Public surface:

  EmailAnalytics(email_db, events_db, verified_users_db_path?)
    .sequence_stats(sequence, day=None, days_back=30)
        Same shape as EmailEventsDB.broadcast_stats but scoped to a
        sequence (and optionally a single day offset within it). Uses
        scheduled_sends.resend_id as the join key. Rows whose resend_id
        is NULL (pre-Phase-1 sends) are excluded so the denominator is
        honest.

    .onboarding_day0_funnel(days_back=30, conversion_window_days=7)
        Top-of-funnel report for the most important email in the system.
        Returns sent → delivered → opened → clicked → telegram_verified
        with per-step counts and rates relative to the prior step. The
        last step joins email_events.recipient → verified_users.email
        with verified_at within ``conversion_window_days`` of sent_at.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import aiosqlite

from src.email_bot.db import EmailDB
from src.email_bot.events_db import EmailEventsDB, _empty_aggregate_stats

logger = logging.getLogger(__name__)


class EmailAnalytics:
    """Read-only analytics that join scheduled_sends + email_events
    (+ verified_users for the Day 0 funnel)."""

    def __init__(
        self,
        email_db: EmailDB,
        events_db: EmailEventsDB,
        verified_users_db_path: str | Path | None = None,
    ):
        self._email_db = email_db
        self._events_db = events_db
        self._verified_path = (
            str(verified_users_db_path) if verified_users_db_path else None
        )

    async def sequence_stats(
        self,
        sequence: str,
        day: int | None = None,
        days_back: int = 30,
        now: int | None = None,
    ) -> dict:
        """Aggregate stats for one sequence over the trailing window.

        Returns the same dict shape as EmailEventsDB.aggregate_by_resend_ids
        plus three extra fields the renderer uses:
          - sequence: echoed back
          - day: echoed back (None if all days)
          - window_seconds: trailing window the query covered
          - sends_with_resend_id: count of scheduled_sends rows that
            contributed (i.e. status='sent' AND resend_id IS NOT NULL).
            This is the denominator the rate fields should be read against.
        """
        ts = int(now if now is not None else time.time())
        since = ts - max(0, int(days_back)) * 86400

        rows = await self._email_db.sent_in_window(
            sequence=sequence,
            day=day,
            since=since,
            until=ts,
        )
        ids = [r["resend_id"] for r in rows if r.get("resend_id")]
        if not ids:
            stats = _empty_aggregate_stats()
        else:
            stats = await self._events_db.aggregate_by_resend_ids(ids)

        # The "sent" count from email_events can lag the actual API send
        # by a few seconds (Resend webhooks are async). For the
        # denominator we trust the email.db row count, which is the
        # authoritative record of what we actually handed to Resend. Then
        # we recompute the rates against that denominator so they don't
        # drift while webhooks are catching up.
        sends_with_resend_id = len(ids)
        if sends_with_resend_id != stats.get("sent", 0):
            # Don't mutate the underlying counts. Just override "sent"
            # for rate purposes; keep the original under sent_events for
            # forensics.
            stats = dict(stats)
            stats["sent_events"] = stats.get("sent", 0)
            stats["sent"] = sends_with_resend_id
            stats["delivery_rate"] = _safe(
                stats.get("delivered", 0), sends_with_resend_id,
            )
            stats["bounce_rate"] = _safe(
                stats.get("bounced", 0), sends_with_resend_id,
            )
            stats["complaint_rate"] = _safe(
                stats.get("complained", 0), sends_with_resend_id,
            )
            stats["fail_rate"] = _safe(
                stats.get("failed", 0), sends_with_resend_id,
            )

        stats = dict(stats)
        stats["sequence"] = sequence
        stats["day"] = day
        stats["window_seconds"] = ts - since
        stats["sends_with_resend_id"] = sends_with_resend_id
        return stats

    async def onboarding_day0_funnel(
        self,
        days_back: int = 30,
        conversion_window_days: int = 7,
        now: int | None = None,
    ) -> dict:
        """Sent → Delivered → Opened → Clicked → Telegram-verified funnel
        for the Day 0 onboarding email.

        For each Day 0 send in the window:
          - sent: row exists in scheduled_sends with status='sent' and
            a non-null resend_id
          - delivered/opened/clicked: at least one matching event in
            email_events for that resend_id
          - telegram_verified: a verified_users row exists where
            LOWER(email) = LOWER(scheduled_sends.email) AND verified_at
            falls in [sent_at, sent_at + conversion_window_days*86400]

        The final step is the only step that requires a Python join
        across three tables. We pull the (email, sent_at) pairs from
        email.db and the (email, verified_at) pairs from verification.db
        once each, then count matches in memory. The verified_users
        table is small (~Telegram-verified Elite members) so this is
        cheap.

        Returns a dict with absolute counts and stepwise rates:
          {
            "sent": N,
            "delivered": N, "delivered_rate": 0.0..1.0,
            "opened":    N, "open_rate":      0.0..1.0,
            "clicked":   N, "click_rate":     0.0..1.0,
            "telegram_verified": N, "verify_rate": 0.0..1.0,
            "window_seconds": int,
            "conversion_window_seconds": int,
          }

        ``verify_rate`` is computed against ``sent`` (top of funnel) so
        the user reads it as "% of recipients who got Day 0 ended up
        Telegram-verified within {conversion_window_days} days." The
        other rates are computed against the prior step.
        """
        ts = int(now if now is not None else time.time())
        since = ts - max(0, int(days_back)) * 86400
        conv_window = max(1, int(conversion_window_days)) * 86400

        rows = await self._email_db.sent_in_window(
            sequence="onboarding",
            day=0,
            since=since,
            until=ts,
        )
        sent_count = len(rows)
        if sent_count == 0:
            return {
                "sent": 0,
                "delivered": 0, "delivered_rate": 0.0,
                "opened": 0, "open_rate": 0.0,
                "clicked": 0, "click_rate": 0.0,
                "telegram_verified": 0, "verify_rate": 0.0,
                "window_seconds": ts - since,
                "conversion_window_seconds": conv_window,
            }

        # Map resend_id -> (email_lower, sent_at) so we can correlate
        # downstream events back to a recipient timestamp for the
        # verified-within-window check.
        sends_by_rid: dict[str, tuple[str, int]] = {}
        for r in rows:
            rid = r.get("resend_id")
            email = (r.get("email") or "").strip().lower()
            sent_at = int(r.get("sent_at") or 0)
            if rid and email and sent_at > 0:
                sends_by_rid[rid] = (email, sent_at)

        ids = list(sends_by_rid.keys())
        agg = await self._events_db.aggregate_by_resend_ids(ids)

        # Step counts derived from the existing aggregate. Funnel reads
        # "at least one event of each type per resend_id"; aggregate_by_
        # resend_ids returns total events, so for "delivered" specifically
        # we want unique resend_ids that had a delivered event. Resend
        # only fires one delivered per email_id so total == unique here,
        # but for opened/clicked we already have unique counts in the
        # aggregate.
        delivered = int(agg.get("delivered", 0))
        opened_unique = int(agg.get("unique_openers", agg.get("opened", 0)))
        clicked_unique = int(agg.get("unique_clickers", agg.get("clicked", 0)))

        # For telegram_verified we need the recipient list AND the time
        # bounds. Pull every verified_users row touched by these
        # recipients, then count those whose verified_at falls in the
        # per-recipient conversion window.
        recipients = {email for email, _ in sends_by_rid.values()}
        verified_count = 0
        if recipients and self._verified_path:
            verified_rows = await self._fetch_verified_emails(recipients)
            # Build a lookup: email_lower -> [verified_at, ...].
            ver_by_email: dict[str, list[int]] = {}
            for em, vat in verified_rows:
                ver_by_email.setdefault(em, []).append(int(vat))
            # For each unique recipient, check if any of their verifications
            # falls in [sent_at, sent_at + conv_window].
            seen_recipients: set[str] = set()
            for email, sent_at in sends_by_rid.values():
                if email in seen_recipients:
                    continue
                seen_recipients.add(email)
                vlist = ver_by_email.get(email, [])
                lo = sent_at
                hi = sent_at + conv_window
                for vat in vlist:
                    if lo <= vat <= hi:
                        verified_count += 1
                        break

        return {
            "sent": sent_count,
            "delivered": delivered,
            "delivered_rate": _safe(delivered, sent_count),
            "opened": opened_unique,
            "open_rate": _safe(opened_unique, delivered),
            "clicked": clicked_unique,
            "click_rate": _safe(clicked_unique, opened_unique),
            "telegram_verified": verified_count,
            "verify_rate": _safe(verified_count, sent_count),
            "window_seconds": ts - since,
            "conversion_window_seconds": conv_window,
        }

    async def _fetch_verified_emails(
        self, emails_lower: set[str],
    ) -> list[tuple[str, int]]:
        """Return (email_lower, verified_at) for every verified_users row
        whose email matches one of the lowercased inputs.

        Opens a fresh aiosqlite connection so the analytics module
        doesn't need to share verification.py's connection pool.
        Read-only.
        """
        if not self._verified_path or not emails_lower:
            return []

        path = Path(self._verified_path)
        if not path.exists():
            logger.warning(
                "EmailAnalytics: verified_users DB not found at %s; "
                "Day 0 funnel will report 0 verifications.",
                path,
            )
            return []

        # SQLite IN clause won't take 121k params, so use a TEMP table
        # the same way EmailEventsDB does.
        async with aiosqlite.connect(str(path)) as conn:
            await conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _funnel_emails "
                "(em TEXT PRIMARY KEY)",
            )
            try:
                await conn.execute("DELETE FROM _funnel_emails")
                await conn.executemany(
                    "INSERT OR IGNORE INTO _funnel_emails (em) VALUES (?)",
                    [(em,) for em in emails_lower if em],
                )
                async with conn.execute(
                    "SELECT LOWER(v.email), v.verified_at "
                    "FROM verified_users v "
                    "JOIN _funnel_emails f ON LOWER(v.email) = f.em "
                    "WHERE v.email IS NOT NULL AND v.email != '' "
                    "  AND v.verified_at > 0",
                ) as cursor:
                    rows = await cursor.fetchall()
            finally:
                try:
                    await conn.execute("DELETE FROM _funnel_emails")
                except Exception:
                    pass
                await conn.commit()
        return [(r[0], int(r[1])) for r in rows]


def _safe(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return float(numerator) / float(denominator)
