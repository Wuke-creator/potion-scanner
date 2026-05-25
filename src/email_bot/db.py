"""SQLite store for email subscribers + scheduled sends.

Two tables:

  subscribers:
    email PRIMARY KEY. Holds user metadata captured at enrollment
    (Whop cancellation event or inactivity trigger). Used by template
    rendering for {name} substitution and by segment-by-reason logic
    at Day 5.

  scheduled_sends:
    Append-only queue. One row per scheduled email. The worker polls
    this table, picks up rows where due_at <= now AND status='pending',
    renders + delivers them, then stamps sent_at and flips status to
    'sent' (or 'failed' with an error message).

A separate DB file (``data/email.db``) so the email subsystem can be
reset without touching user verification or analytics.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass
class Subscriber:
    email: str
    name: str
    trigger_type: str        # 'cancellation' or 'inactivity'
    exit_reason: str         # one of: too_expensive, not_using, market_slow, quality_declined, found_alternative, other, fulfillment, none
    created_at: int
    rejoin_url: str = ""     # optional per-user tracking URL


@dataclass
class ScheduledSend:
    id: int
    email: str
    sequence: str            # 'winback' or 'reengagement'
    day: int                 # 1, 3, 5, or 7
    due_at: int              # epoch seconds
    sent_at: int | None
    status: str              # pending | sent | failed | canceled
    error: str | None
    resend_id: str | None = None  # Resend message id, set when worker delivers


# Valid exit reason codes. Keep in sync with template offer variants.
EXIT_REASONS = {
    "too_expensive",       # Offer A
    "not_using",           # Offer B
    "market_slow",         # Offer C
    "quality_declined",    # Offer D
    "found_alternative",   # Offer E
    "other",               # Offer F
    "fulfillment",         # Offer F (treated as 'other' for email copy)
    "none",                # re-engagement / inactivity: no exit reason
}


_SUBSCRIBERS_DDL = """
CREATE TABLE IF NOT EXISTS subscribers (
  email         TEXT PRIMARY KEY,
  name          TEXT NOT NULL DEFAULT '',
  trigger_type  TEXT NOT NULL,
  exit_reason   TEXT NOT NULL DEFAULT 'none',
  rejoin_url    TEXT NOT NULL DEFAULT '',
  created_at    INTEGER NOT NULL
);
"""

_SENDS_DDL = """
CREATE TABLE IF NOT EXISTS scheduled_sends (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  email      TEXT NOT NULL,
  sequence   TEXT NOT NULL,
  day        INTEGER NOT NULL,
  due_at     INTEGER NOT NULL,
  sent_at    INTEGER,
  status     TEXT NOT NULL DEFAULT 'pending',
  error      TEXT,
  resend_id  TEXT
);
"""

# Migrations for DBs that predate the resend_id column. SQLite errors if
# the column already exists; the open() helper swallows that case.
_SENDS_MIGRATIONS = (
    "ALTER TABLE scheduled_sends ADD COLUMN resend_id TEXT",
)

_SENDS_DUE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_sends_due
    ON scheduled_sends (status, due_at);
"""

_SENDS_EMAIL_INDEX = """
CREATE INDEX IF NOT EXISTS idx_sends_email
    ON scheduled_sends (email);
"""

_SENDS_RESEND_INDEX = """
CREATE INDEX IF NOT EXISTS idx_sends_resend_id
    ON scheduled_sends (resend_id);
"""

_SENDS_SEQ_DAY_SENT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_sends_seq_day_sent
    ON scheduled_sends (sequence, day, sent_at);
"""


class EmailDB:
    """Async SQLite wrapper for the email subsystem."""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(_SUBSCRIBERS_DDL)
        await self._conn.execute(_SENDS_DDL)
        for stmt in _SENDS_MIGRATIONS:
            try:
                await self._conn.execute(stmt)
            except Exception:
                # Column already exists from a prior migration run.
                pass
        await self._conn.execute(_SENDS_DUE_INDEX)
        await self._conn.execute(_SENDS_EMAIL_INDEX)
        await self._conn.execute(_SENDS_RESEND_INDEX)
        await self._conn.execute(_SENDS_SEQ_DAY_SENT_INDEX)
        await self._conn.commit()
        logger.info("Email DB opened at %s", self._db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---- subscribers --------------------------------------------------

    async def upsert_subscriber(self, sub: Subscriber) -> None:
        """Insert or update a subscriber row. Resets on re-enrollment."""
        assert self._conn is not None
        if sub.exit_reason not in EXIT_REASONS:
            raise ValueError(f"unknown exit_reason: {sub.exit_reason!r}")
        await self._conn.execute(
            "INSERT INTO subscribers "
            "(email, name, trigger_type, exit_reason, rejoin_url, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "  name = excluded.name, "
            "  trigger_type = excluded.trigger_type, "
            "  exit_reason = excluded.exit_reason, "
            "  rejoin_url = excluded.rejoin_url, "
            "  created_at = excluded.created_at",
            (
                sub.email, sub.name, sub.trigger_type, sub.exit_reason,
                sub.rejoin_url, sub.created_at,
            ),
        )
        await self._conn.commit()

    async def get_subscriber(self, email: str) -> Subscriber | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT email, name, trigger_type, exit_reason, rejoin_url, created_at "
            "FROM subscribers WHERE email = ?",
            (email,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return Subscriber(
            email=row[0],
            name=row[1],
            trigger_type=row[2],
            exit_reason=row[3],
            rejoin_url=row[4],
            created_at=row[5],
        )

    # ---- scheduled_sends ----------------------------------------------

    # Known sequence names. New sequences must be registered here so the
    # render pipeline knows about them. Add a row to KNOWN_SEQUENCES and a
    # matching renderer in templates._ONBOARDING_RENDERERS / etc.
    KNOWN_SEQUENCES = {
        # Cancellation / churn-prevention
        "winback",            # 3-email Day 1/4/7 sequence on cancel
        "reengagement",       # 3-email Day 1/4/7 sequence on inactivity
        # Lifecycle (new 2026-04-27)
        "onboarding",         # 5 emails Day 0/3/5/7/30 + monthly digest
        "dunning",            # 3 emails Day 0/3/10 on payment failure
        "pre_renewal",        # one-shot 3 days before billing
        "pre_pause_return",   # one-shot 3 days before pause expiry
        "inactive_day10",     # one-shot at 10 days of inactivity
        # AUT-026 Targeted Save Offer (one-shot, fires the moment Whop
        # signals a cancellation so we hit the user with a personalised
        # offer BEFORE the standard winback Day 1 lands)
        "save_offer",         # one-shot, immediate, copy varies by exit reason
        # AUT-033 Post-Retention Follow-Up Survey (one-shot, fires 7 days
        # after a cancelled member reactivates — the "save was successful,
        # what convinced you to stay?" survey).
        "post_retention",
        # Bronze -> Elite upsell. Originally days 1/3/5, extended to
        # 0/1/3/5/7 by the 2026-05-19 behaviour-tree work. Day 5 carries
        # the single-use 30%-off Elite promo minted at send time.
        # Independent lifecycle (not cancel-on-reschedule).
        "bronze",
        # Behaviour-tree additions (2026-05-19):
        #   nurture       = event-driven big-call gap email for
        #                   non-converted Bronze (one-shot per fire,
        #                   cron filters by P&L threshold).
        #   paid_at_risk  = 2-email D0/D3 catch-churn-before-cancel
        #                   sequence for paid Elite members who go
        #                   quiet in Discord 7-14d before renewal.
        "nurture",
        "paid_at_risk",
    }

    # Sequences that are mutually exclusive when scheduling — a fresh
    # winback or reengagement cancels prior pending sends from the SAME
    # category. Onboarding / dunning / one-shots are independent: a new
    # member can hit dunning on their first cycle without their onboarding
    # sequence getting cancelled.
    _CANCEL_ON_RESCHEDULE = {"winback", "reengagement"}

    async def schedule_sequence(
        self,
        email: str,
        sequence: str,
        day_offsets: tuple[int, ...] | None = None,
        now: int | None = None,
    ) -> list[int]:
        """Queue the email sequence for a subscriber.

        For winback/reengagement: cancels any pending sends from prior
        sequences for the same email so a user who re-cancels after
        re-joining gets a fresh sequence instead of overlapping delivery.

        For onboarding/dunning/one-shots: does NOT cancel prior pending
        sends — they're independent lifecycles that can legitimately
        overlap (e.g. a member can be in onboarding day 7 AND get a
        dunning day 0 the same week).

        Per-sequence defaults (both simplified to 3 emails 2026-04-18):
          winback: day 1 (soft touch), 4 (offer), 7 (last chance)
          reengagement: day 1 (miss you), 4 (what you missed), 7 (personal touch)
          onboarding: 0 / 3 / 5 / 7 / 30 (5 emails)
          dunning: 0 / 3 / 10 (3 emails — Day 7 is Discord, out of scope)

        Returns the list of inserted send IDs.
        """
        assert self._conn is not None
        if sequence not in self.KNOWN_SEQUENCES:
            raise ValueError(f"unknown sequence: {sequence!r}")
        if day_offsets is None:
            day_offsets = self._default_offsets(sequence)
        now = now if now is not None else int(time.time())

        # Cancel-on-reschedule: winback and reengagement are mutually
        # exclusive churn flows, so enrolling in either one cancels any
        # pending sends from EITHER. New lifecycle sequences (onboarding,
        # dunning, etc.) don't touch existing pending sends — a member
        # can legitimately be in onboarding and dunning concurrently.
        if sequence in self._CANCEL_ON_RESCHEDULE:
            await self._conn.execute(
                "UPDATE scheduled_sends SET status='canceled' "
                "WHERE email = ? AND status = 'pending' "
                "  AND sequence IN ('winback', 'reengagement')",
                (email,),
            )

        send_ids: list[int] = []
        for day in day_offsets:
            due_at = now + day * 86400
            cursor = await self._conn.execute(
                "INSERT INTO scheduled_sends "
                "(email, sequence, day, due_at, status) "
                "VALUES (?, ?, ?, ?, 'pending')",
                (email, sequence, day, due_at),
            )
            send_ids.append(cursor.lastrowid or 0)
        await self._conn.commit()
        return send_ids

    @staticmethod
    def _default_offsets(sequence: str) -> tuple[int, ...]:
        """Default day offsets per sequence."""
        if sequence in ("winback", "reengagement"):
            return (1, 4, 7)
        if sequence == "onboarding":
            return (0, 3, 5, 7, 30)
        if sequence == "dunning":
            return (0, 3, 10)
        if sequence == "bronze":
            # Extended 2026-05-19 from (1, 3, 5) to (0, 1, 3, 5, 7):
            # D0 welcomes + drives first Discord visit before the
            # upsell starts; D7 is the last-call urgency on the
            # BRONZE30 code (still minted at D5). The branch evaluator
            # picks HOT/WARM variants at D3/D5 at send time.
            return (0, 1, 3, 5, 7)
        if sequence == "paid_at_risk":
            return (0, 3)
        # One-shot sequences (pre_renewal etc.) get one send at the
        # caller-specified due_at via schedule_one — schedule_sequence
        # isn't the right entry point. Default to (0,) defensively.
        return (0,)

    async def schedule_one(
        self,
        email: str,
        sequence: str,
        day: int,
        due_at: int | None = None,
    ) -> int:
        """Queue a single send (used by admin test endpoint AND by
        one-shot sequences like pre_renewal / pre_pause_return / inactive_day10)."""
        assert self._conn is not None
        if sequence not in self.KNOWN_SEQUENCES:
            raise ValueError(f"unknown sequence: {sequence!r}")
        when = due_at if due_at is not None else int(time.time())
        cursor = await self._conn.execute(
            "INSERT INTO scheduled_sends "
            "(email, sequence, day, due_at, status) "
            "VALUES (?, ?, ?, ?, 'pending')",
            (email, sequence, day, when),
        )
        await self._conn.commit()
        return cursor.lastrowid or 0

    async def cancel_pending(self, email: str, sequence: str) -> int:
        """Cancel every still-pending send for ``email`` in ``sequence``.

        Used by the Bronze upgrade-stop: when a free member converts to a
        paid plan we cancel their remaining bronze sends so they never
        get the "30% off Elite" offer after they've already upgraded.
        Returns the number of sends cancelled (0 = nothing pending).
        """
        assert self._conn is not None
        cur = await self._conn.execute(
            "UPDATE scheduled_sends SET status='canceled' "
            "WHERE email = ? AND sequence = ? AND status = 'pending'",
            (email, sequence),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def cancel_all_pending(self, email: str) -> int:
        """Cancel every still-pending send for ``email`` across ALL sequences.

        Used by the Resend bounce/complaint suppression path
        (``_maybe_suppress`` in webhook.py): when a recipient bounces
        or complains, we mark whop_members.valid=0 (blocks future
        enrollment) AND cancel any in-flight scheduled rows so the
        worker does not deliver another email to that recipient.
        Returns the number of sends cancelled (0 = nothing pending).
        """
        assert self._conn is not None
        cur = await self._conn.execute(
            "UPDATE scheduled_sends SET status='canceled' "
            "WHERE email = ? AND status = 'pending'",
            (email,),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def due_sends(self, now: int | None = None) -> list[ScheduledSend]:
        """Return all pending sends whose due_at has passed."""
        assert self._conn is not None
        now = now if now is not None else int(time.time())
        async with self._conn.execute(
            "SELECT id, email, sequence, day, due_at, sent_at, status, "
            "       error, resend_id "
            "FROM scheduled_sends "
            "WHERE status = 'pending' AND due_at <= ? "
            "ORDER BY due_at ASC "
            "LIMIT 500",
            (now,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            ScheduledSend(
                id=r[0], email=r[1], sequence=r[2], day=r[3],
                due_at=r[4], sent_at=r[5], status=r[6], error=r[7],
                resend_id=r[8],
            )
            for r in rows
        ]

    async def mark_sent(
        self, send_id: int, resend_id: str | None = None,
    ) -> None:
        """Mark a send as delivered. ``resend_id`` is the message id Resend
        returns from POST /emails; we persist it so webhook events
        (opened/clicked/bounced) can be joined back to the (sequence, day)
        tuple by the analytics layer.
        """
        assert self._conn is not None
        clean_id = (resend_id or "").strip() or None
        await self._conn.execute(
            "UPDATE scheduled_sends "
            "SET status='sent', sent_at=?, error=NULL, "
            "    resend_id=COALESCE(?, resend_id) "
            "WHERE id = ?",
            (int(time.time()), clean_id, send_id),
        )
        await self._conn.commit()

    async def sent_in_window(
        self,
        sequence: str | None = None,
        day: int | None = None,
        since: int | None = None,
        until: int | None = None,
        limit: int = 200_000,
    ) -> list[dict]:
        """Sends marked status='sent' in the given window, optionally
        filtered by sequence + day. Returns dicts with the join-key
        fields the analytics layer needs.

        Rows whose ``resend_id`` is NULL are skipped: those are pre-Phase-1
        sends that we have no way to match against email_events, so they
        would only inflate the 'sent' denominator without contributing to
        any open/click/bounce counts. The fallback path for those rows is
        recipient-level history, which we expose separately.
        """
        assert self._conn is not None
        clauses = ["status = 'sent'", "resend_id IS NOT NULL", "resend_id != ''"]
        params: list = []
        if sequence is not None:
            clauses.append("sequence = ?")
            params.append(sequence)
        if day is not None:
            clauses.append("day = ?")
            params.append(int(day))
        if since is not None:
            clauses.append("sent_at >= ?")
            params.append(int(since))
        if until is not None:
            clauses.append("sent_at <= ?")
            params.append(int(until))
        sql = (
            "SELECT id, email, sequence, day, due_at, sent_at, resend_id "
            "FROM scheduled_sends "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY sent_at DESC "
            "LIMIT ?"
        )
        params.append(int(limit))
        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "id": r[0],
                "email": r[1],
                "sequence": r[2],
                "day": r[3],
                "due_at": r[4],
                "sent_at": r[5],
                "resend_id": r[6],
            }
            for r in rows
        ]

    async def mark_canceled(self, send_id: int, reason: str) -> None:
        """Cancel a single in-flight send by id with a reason. Used by the
        worker's last-mile suppression check: even after a send row was
        claimed from ``due_sends()``, the recipient may have bounced /
        complained / unsubscribed in the window between claim and
        delivery. We cancel rather than mark_failed because this is not
        a delivery failure -- it is a deliberate refusal to send.
        """
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE scheduled_sends "
            "SET status='canceled', sent_at=?, error=? WHERE id = ?",
            (int(time.time()), reason[:500], send_id),
        )
        await self._conn.commit()

    async def mark_failed(self, send_id: int, error: str) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE scheduled_sends "
            "SET status='failed', sent_at=?, error=? WHERE id = ?",
            (int(time.time()), error[:500], send_id),
        )
        await self._conn.commit()

    async def count_by_status(self) -> dict[str, int]:
        """Summary counts for /admin style endpoints."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT status, COUNT(*) FROM scheduled_sends GROUP BY status"
        ) as cursor:
            rows = await cursor.fetchall()
        return {r[0]: int(r[1]) for r in rows}
