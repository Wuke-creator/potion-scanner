"""SQLite store for Resend webhook events.

A parallel observability layer for the email subsystem. The send pipeline
in ``worker.py`` / ``sender.py`` is unchanged; this DB only records what
Resend tells us about every email after it leaves our hands.

One table:

  email_events:
    Append-only event log. One row per (resend_email_id, event_type,
    event_at) tuple. Idempotent on Resend retries via the UNIQUE
    constraint plus ``INSERT OR IGNORE``.

Used for:

  - Auto-suppression: ``hard_bounces_in_window`` + ``soft_bounce_count``
    feed the webhook handler's decision to flip ``whop_members.valid=0``.
  - Per-broadcast reporting: ``broadcast_stats`` aggregates counts and
    rates for the ``/email-broadcast-stats`` Discord slash command.
  - Forensics: ``raw_payload`` keeps the full JSON Resend sent us so we
    can replay decisions if a suppression rule gets disputed.

Separate DB file (``data/email_events.db``) so high-volume webhook
traffic can't lock the small subscribers / scheduled_sends DB during
peak broadcast windows.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


# Eight Resend event types (see https://resend.com/docs/dashboard/webhooks/event-types).
# Stored without the 'email.' prefix to keep column values terse.
KNOWN_EVENT_TYPES = frozenset({
    "sent",
    "delivered",
    "delivery_delayed",
    "complained",
    "bounced",
    "opened",
    "clicked",
    "failed",
})


_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS email_events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  resend_email_id TEXT NOT NULL,
  broadcast_id    TEXT NOT NULL DEFAULT '',
  recipient       TEXT NOT NULL,
  event_type      TEXT NOT NULL,
  event_at        INTEGER NOT NULL,
  click_url       TEXT,
  bounce_type     TEXT,
  bounce_message  TEXT,
  raw_payload     TEXT NOT NULL,
  UNIQUE(resend_email_id, event_type, event_at)
);
"""

_EVENTS_BROADCAST_INDEX = """
CREATE INDEX IF NOT EXISTS idx_events_broadcast
    ON email_events(broadcast_id, event_type);
"""

_EVENTS_RECIPIENT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_events_recipient
    ON email_events(recipient, event_type);
"""

# Unsubscribe ledger. Created here so the suppression gate
# (``is_suppressed_recipient``) can query it without crashing on a
# fresh DB. Production already has this table; ``IF NOT EXISTS`` makes
# the statement a no-op there.
_UNSUBS_DDL = """
CREATE TABLE IF NOT EXISTS email_unsubscribes (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  recipient        TEXT NOT NULL,
  recipient_domain TEXT NOT NULL DEFAULT '',
  source           TEXT NOT NULL DEFAULT '',
  resend_email_id  TEXT NOT NULL DEFAULT '',
  unsubscribed_at  INTEGER NOT NULL,
  ip_address       TEXT,
  user_agent       TEXT,
  UNIQUE(recipient)
);
"""


class EmailEventsDB:
    """Async SQLite wrapper for the Resend webhook event store."""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(_EVENTS_DDL)
        await self._conn.execute(_EVENTS_BROADCAST_INDEX)
        await self._conn.execute(_EVENTS_RECIPIENT_INDEX)
        await self._conn.execute(_UNSUBS_DDL)
        await self._conn.commit()
        logger.info("Email events DB opened at %s", self._db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---- writes -------------------------------------------------------

    async def record_event(
        self,
        *,
        resend_email_id: str,
        broadcast_id: str,
        recipient: str,
        event_type: str,
        event_at: int,
        click_url: str | None = None,
        bounce_type: str | None = None,
        bounce_message: str | None = None,
        raw_payload: str = "",
    ) -> bool:
        """Insert one event. Returns True if a new row was inserted, False
        if the (email_id, type, event_at) tuple already existed (Resend
        retry on a non-200 we've actually already processed).
        """
        assert self._conn is not None
        cur = await self._conn.execute(
            "INSERT OR IGNORE INTO email_events "
            "(resend_email_id, broadcast_id, recipient, event_type, "
            " event_at, click_url, bounce_type, bounce_message, raw_payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                resend_email_id, broadcast_id, recipient, event_type,
                event_at, click_url, bounce_type, bounce_message, raw_payload,
            ),
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    # ---- reads --------------------------------------------------------

    async def broadcast_stats(self, broadcast_id: str) -> dict:
        """Aggregate counts + rates + top click URLs for one broadcast.

        Rates are computed against the canonical denominators Resend uses
        in its dashboard:
          - delivered_rate / bounce_rate / complaint_rate / fail_rate vs sent
          - open_rate vs delivered (you can't open what didn't arrive)
          - click_rate (delivered) vs delivered, click_rate (opens) vs opened

        All rates guard against zero divisor (returns 0.0).
        """
        assert self._conn is not None

        counts: dict[str, int] = {t: 0 for t in KNOWN_EVENT_TYPES}
        async with self._conn.execute(
            "SELECT event_type, COUNT(*) FROM email_events "
            "WHERE broadcast_id = ? GROUP BY event_type",
            (broadcast_id,),
        ) as cursor:
            async for row in cursor:
                if row[0] in counts:
                    counts[row[0]] = int(row[1])

        # Hard / soft split inside bounced.
        hard_bounced = 0
        soft_bounced = 0
        async with self._conn.execute(
            "SELECT COALESCE(LOWER(bounce_type), ''), COUNT(*) "
            "FROM email_events "
            "WHERE broadcast_id = ? AND event_type = 'bounced' "
            "GROUP BY COALESCE(LOWER(bounce_type), '')",
            (broadcast_id,),
        ) as cursor:
            async for row in cursor:
                bt = row[0]
                n = int(row[1])
                if bt == "hard":
                    hard_bounced = n
                elif bt == "soft":
                    soft_bounced = n

        # Top click URLs (limit 10).
        top_clicks: list[dict] = []
        async with self._conn.execute(
            "SELECT click_url, COUNT(*) AS c FROM email_events "
            "WHERE broadcast_id = ? AND event_type = 'clicked' "
            "  AND click_url IS NOT NULL AND click_url != '' "
            "GROUP BY click_url ORDER BY c DESC, click_url ASC LIMIT 10",
            (broadcast_id,),
        ) as cursor:
            async for row in cursor:
                top_clicks.append({"url": row[0], "count": int(row[1])})

        sent = counts["sent"]
        delivered = counts["delivered"]
        opened = counts["opened"]
        clicked = counts["clicked"]
        bounced = counts["bounced"]
        complained = counts["complained"]
        failed = counts["failed"]

        return {
            "sent": sent,
            "delivered": delivered,
            "opened": opened,
            "clicked": clicked,
            "bounced": bounced,
            "complained": complained,
            "failed": failed,
            "delivery_delayed": counts["delivery_delayed"],
            "hard_bounced": hard_bounced,
            "soft_bounced": soft_bounced,
            "delivery_rate": _safe_rate(delivered, sent),
            "open_rate": _safe_rate(opened, delivered),
            "click_rate_delivered": _safe_rate(clicked, delivered),
            "click_rate_opened": _safe_rate(clicked, opened),
            "bounce_rate": _safe_rate(bounced, sent),
            "complaint_rate": _safe_rate(complained, sent),
            "fail_rate": _safe_rate(failed, sent),
            "top_clicked_urls": top_clicks,
        }

    async def aggregate_by_resend_ids(
        self, resend_ids: list[str] | set[str],
    ) -> dict:
        """Same shape as ``broadcast_stats`` but for an arbitrary set of
        resend message ids. Used by the analytics layer to compute
        sequence-level stats by joining ``scheduled_sends.resend_id`` →
        ``email_events.resend_email_id``.

        Loads ids into a TEMP table so the IN clause scales past
        SQLite's 999-parameter limit. The caller is expected to pass the
        resend_ids of sends whose 'sent' event we want to count as the
        denominator.
        """
        assert self._conn is not None
        ids = [rid for rid in resend_ids if rid]
        if not ids:
            return _empty_aggregate_stats()

        # Per-call temp table. aiosqlite serializes queries on a single
        # connection so concurrent callers can't race here, but we still
        # DROP the table on the way out so a query that explodes
        # mid-flight doesn't leave stale rows around for the next call.
        await self._conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _agg_ids (rid TEXT PRIMARY KEY)",
        )
        try:
            await self._conn.execute("DELETE FROM _agg_ids")
            await self._conn.executemany(
                "INSERT OR IGNORE INTO _agg_ids (rid) VALUES (?)",
                [(rid,) for rid in ids],
            )

            counts: dict[str, int] = {t: 0 for t in KNOWN_EVENT_TYPES}
            async with self._conn.execute(
                "SELECT e.event_type, COUNT(*) "
                "FROM email_events e "
                "JOIN _agg_ids t ON e.resend_email_id = t.rid "
                "GROUP BY e.event_type",
            ) as cursor:
                async for row in cursor:
                    if row[0] in counts:
                        counts[row[0]] = int(row[1])

            hard_bounced = 0
            soft_bounced = 0
            async with self._conn.execute(
                "SELECT COALESCE(LOWER(e.bounce_type), ''), COUNT(*) "
                "FROM email_events e "
                "JOIN _agg_ids t ON e.resend_email_id = t.rid "
                "WHERE e.event_type = 'bounced' "
                "GROUP BY COALESCE(LOWER(e.bounce_type), '')",
            ) as cursor:
                async for row in cursor:
                    bt = row[0]
                    n = int(row[1])
                    if bt == "hard":
                        hard_bounced = n
                    elif bt == "soft":
                        soft_bounced = n

            top_clicks: list[dict] = []
            async with self._conn.execute(
                "SELECT e.click_url, COUNT(*) AS c "
                "FROM email_events e "
                "JOIN _agg_ids t ON e.resend_email_id = t.rid "
                "WHERE e.event_type = 'clicked' "
                "  AND e.click_url IS NOT NULL AND e.click_url != '' "
                "GROUP BY e.click_url "
                "ORDER BY c DESC, e.click_url ASC LIMIT 10",
            ) as cursor:
                async for row in cursor:
                    top_clicks.append(
                        {"url": row[0], "count": int(row[1])},
                    )

            # We could also count distinct openers and clickers; useful
            # because a single recipient can fire multiple opens. Done in
            # one extra round-trip to keep the surface narrow.
            unique_openers = 0
            unique_clickers = 0
            async with self._conn.execute(
                "SELECT COUNT(DISTINCT e.recipient) "
                "FROM email_events e "
                "JOIN _agg_ids t ON e.resend_email_id = t.rid "
                "WHERE e.event_type = 'opened'",
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    unique_openers = int(row[0] or 0)
            async with self._conn.execute(
                "SELECT COUNT(DISTINCT e.recipient) "
                "FROM email_events e "
                "JOIN _agg_ids t ON e.resend_email_id = t.rid "
                "WHERE e.event_type = 'clicked'",
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    unique_clickers = int(row[0] or 0)
        finally:
            try:
                await self._conn.execute("DELETE FROM _agg_ids")
            except Exception:
                logger.exception("aggregate_by_resend_ids: cleanup failed")
            await self._conn.commit()

        sent = counts["sent"]
        delivered = counts["delivered"]
        opened = counts["opened"]
        clicked = counts["clicked"]
        bounced = counts["bounced"]
        complained = counts["complained"]
        failed = counts["failed"]

        return {
            "sent": sent,
            "delivered": delivered,
            "opened": opened,
            "clicked": clicked,
            "bounced": bounced,
            "complained": complained,
            "failed": failed,
            "delivery_delayed": counts["delivery_delayed"],
            "hard_bounced": hard_bounced,
            "soft_bounced": soft_bounced,
            "unique_openers": unique_openers,
            "unique_clickers": unique_clickers,
            "delivery_rate": _safe_rate(delivered, sent),
            "open_rate": _safe_rate(opened, delivered),
            "click_rate_delivered": _safe_rate(clicked, delivered),
            "click_rate_opened": _safe_rate(clicked, opened),
            "bounce_rate": _safe_rate(bounced, sent),
            "complaint_rate": _safe_rate(complained, sent),
            "fail_rate": _safe_rate(failed, sent),
            "top_clicked_urls": top_clicks,
        }

    async def recipient_history(self, email: str) -> list[dict]:
        """Every event for a recipient, newest first. For ad-hoc
        debugging when a member complains they got the wrong email."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT resend_email_id, broadcast_id, event_type, event_at, "
            "       click_url, bounce_type, bounce_message "
            "FROM email_events WHERE LOWER(recipient) = LOWER(?) "
            "ORDER BY event_at DESC LIMIT 200",
            (email,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "resend_email_id": r[0],
                "broadcast_id": r[1],
                "event_type": r[2],
                "event_at": r[3],
                "click_url": r[4],
                "bounce_type": r[5],
                "bounce_message": r[6],
            }
            for r in rows
        ]

    async def hard_bounces_in_window(self, seconds: int) -> list[str]:
        """Recipients who hard-bounced in the last `seconds`. Used for
        ad-hoc "who has Resend rejected lately" audits."""
        assert self._conn is not None
        cutoff = int(time.time()) - max(0, int(seconds))
        async with self._conn.execute(
            "SELECT DISTINCT recipient FROM email_events "
            "WHERE event_type = 'bounced' AND LOWER(bounce_type) = 'hard' "
            "  AND event_at >= ?",
            (cutoff,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [r[0] for r in rows if r[0]]

    async def soft_bounce_count(
        self, recipient: str, *, since_epoch: int,
    ) -> int:
        """How many soft bounces this recipient has had since `since_epoch`.
        Used by the 3-in-30-days suppression rule."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COUNT(*) FROM email_events "
            "WHERE LOWER(recipient) = LOWER(?) "
            "  AND event_type = 'bounced' "
            "  AND LOWER(bounce_type) = 'soft' "
            "  AND event_at >= ?",
            (recipient, int(since_epoch)),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def is_suppressed_recipient(
        self, email: str,
    ) -> tuple[bool, str | None]:
        """Last-mile suppression gate: should we refuse to send to this
        recipient right now?

        Returns ``(True, reason)`` if any of:
          - the recipient has any prior ``bounced`` event recorded
            (hard OR soft -- per the 2026-05-19 policy, one bounce is
            enough forever)
          - the recipient has any prior ``complained`` event recorded
          - the recipient is in ``email_unsubscribes`` (List-Unsubscribe
            click or manual operator action)

        Returns ``(False, None)`` otherwise.

        Designed for callers in the actual send path
        (``EmailWorker._deliver_one``, broadcast loops) as a defense in
        depth against the race window between a bounce webhook arriving
        and ``cancel_all_pending`` reaching the in-flight row. The check
        is case-insensitive on email.

        Cheap: both queries are O(log n) on the recipient index already
        present on ``email_events`` and the UNIQUE constraint on
        ``email_unsubscribes.recipient``.
        """
        assert self._conn is not None
        if not email:
            return False, None
        e = email.strip()
        if not e:
            return False, None

        # Bounce / complaint events.
        async with self._conn.execute(
            "SELECT event_type, bounce_type FROM email_events "
            "WHERE LOWER(recipient) = LOWER(?) "
            "  AND event_type IN ('bounced', 'complained') "
            "ORDER BY event_at ASC LIMIT 1",
            (e,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is not None:
            event_type = str(row[0])
            if event_type == "complained":
                return True, "complaint"
            bounce_type = (row[1] or "unknown").lower()
            return True, f"{bounce_type} bounce"

        # List-Unsubscribe / manual unsub.
        async with self._conn.execute(
            "SELECT 1 FROM email_unsubscribes "
            "WHERE LOWER(recipient) = LOWER(?) LIMIT 1",
            (e,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is not None:
            return True, "unsubscribed"

        return False, None

    async def cleanup_older_than(self, seconds: int) -> int:
        """Drop event rows older than `seconds` ago. Returns rowcount.
        Not wired to a cron yet; here so a future retention policy doesn't
        need a schema migration."""
        assert self._conn is not None
        cutoff = int(time.time()) - max(0, int(seconds))
        cur = await self._conn.execute(
            "DELETE FROM email_events WHERE event_at < ?", (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0


def _safe_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return float(numerator) / float(denominator)


def _empty_aggregate_stats() -> dict:
    """Zero-filled stats dict for an empty resend_id set. Same shape as
    aggregate_by_resend_ids() so callers can render uniformly."""
    return {
        "sent": 0,
        "delivered": 0,
        "opened": 0,
        "clicked": 0,
        "bounced": 0,
        "complained": 0,
        "failed": 0,
        "delivery_delayed": 0,
        "hard_bounced": 0,
        "soft_bounced": 0,
        "unique_openers": 0,
        "unique_clickers": 0,
        "delivery_rate": 0.0,
        "open_rate": 0.0,
        "click_rate_delivered": 0.0,
        "click_rate_opened": 0.0,
        "bounce_rate": 0.0,
        "complaint_rate": 0.0,
        "fail_rate": 0.0,
        "top_clicked_urls": [],
    }
