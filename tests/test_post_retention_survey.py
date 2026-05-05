"""Tests for AUT-033 Post-Retention Follow-Up Survey scheduling.

The trigger lives inside webhook.py:_dispatch_reactivation. When a
membership.activated event arrives AND the email_db carries a recent
'cancellation' subscriber row for the same email, we schedule the
post_retention sequence at day=0 with a due_at of NOW + 7 days.

These tests drive the dispatcher directly via _dispatch_reactivation
so we don't have to fake a Whop signature.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from src.email_bot.db import EmailDB, Subscriber
from src.email_bot.events_db import EmailEventsDB
from src.email_bot.stats import StatsBundle
from src.email_bot.templates import render
from src.email_bot.webhook import EmailWebhookHandlers


def _make_stats() -> StatsBundle:
    return StatsBundle(
        calls_7d_total=0,
        wins_7d_over_50pct=0,
        top_call_7d=None,
        top_calls_7d=[],
        calls_30d_total=0,
        top_call_30d=None,
        top_calls_30d=[],
    )


@pytest.mark.asyncio
class TestPostRetentionScheduling:
    async def _make_handlers(
        self, tmp_path, *, survey_url: str = "https://forms.gle/example",
    ) -> tuple[EmailWebhookHandlers, EmailDB]:
        email_db = EmailDB(db_path=str(tmp_path / "post_retention.db"))
        await email_db.open()
        members_db = AsyncMock()
        members_db.stop_dunning = AsyncMock(return_value=None)
        h = EmailWebhookHandlers(
            db=email_db,
            whop_webhook_secret="not-used-in-this-test",
            admin_secret="x",
            rejoin_url_default="https://whop.com/potion",
            whop_members_db=members_db,
            post_retention_survey_url=survey_url,
            post_retention_delay_days=7,
            post_retention_max_lookback_days=30,
        )
        return h, email_db

    async def test_reactivation_after_recent_cancellation_schedules_survey(
        self, tmp_path,
    ):
        h, db = await self._make_handlers(tmp_path)
        # Pre-seed: this user cancelled 3 days ago (well within lookback)
        await db.upsert_subscriber(Subscriber(
            email="saved@example.com",
            name="Saved User",
            trigger_type="cancellation",
            exit_reason="too_expensive",
            rejoin_url="https://whop.com/potion?promo=STAY79-XXX",
            created_at=int(time.time()) - 3 * 86400,
        ))
        # Reactivation arrives
        resp = await h._dispatch_reactivation({
            "user": {"email": "saved@example.com", "id": "user_123"},
        })
        assert resp.status == 200
        # Survey scheduled for ~7 days out
        sub = await db.get_subscriber("saved@example.com")
        assert sub is not None
        assert sub.trigger_type == "post_retention"
        assert sub.rejoin_url == "https://forms.gle/example"
        # The post_retention send is scheduled in the future, so it
        # won't show up in due_sends(now) but should exist in the table.
        future = int(time.time()) + 8 * 86400
        sends = await db.due_sends(now=future)
        survey_sends = [s for s in sends if s.sequence == "post_retention"]
        assert len(survey_sends) == 1
        assert survey_sends[0].day == 0
        await db.close()

    async def test_reactivation_without_prior_cancellation_no_op(
        self, tmp_path,
    ):
        """First-time joiners (no prior cancellation row) should NOT
        get a post-retention survey — they have nothing to comment on."""
        h, db = await self._make_handlers(tmp_path)
        resp = await h._dispatch_reactivation({
            "user": {"email": "new@example.com", "id": "user_999"},
        })
        assert resp.status == 200
        sub = await db.get_subscriber("new@example.com")
        assert sub is None  # no upsert happened
        sends = await db.due_sends(now=int(time.time()) + 30 * 86400)
        assert sends == []
        await db.close()

    async def test_old_cancellation_outside_lookback_no_op(self, tmp_path):
        """A user who cancelled 60 days ago and rejoins is essentially a
        fresh signup, not a save. Don't ask them what made them stay."""
        h, db = await self._make_handlers(tmp_path)
        await db.upsert_subscriber(Subscriber(
            email="oldcancel@example.com",
            name="",
            trigger_type="cancellation",
            exit_reason="other",
            rejoin_url="",
            created_at=int(time.time()) - 60 * 86400,
        ))
        resp = await h._dispatch_reactivation({
            "user": {"email": "oldcancel@example.com", "id": "user_111"},
        })
        assert resp.status == 200
        # Survey NOT scheduled
        sends = await db.due_sends(now=int(time.time()) + 30 * 86400)
        survey_sends = [s for s in sends if s.sequence == "post_retention"]
        assert survey_sends == []
        await db.close()

    async def test_disabled_when_survey_url_unset(self, tmp_path):
        """If POST_RETENTION_SURVEY_URL isn't configured, the scheduling
        path skips entirely so we never queue an email with a dead CTA."""
        h, db = await self._make_handlers(tmp_path, survey_url="")
        await db.upsert_subscriber(Subscriber(
            email="disabled@example.com",
            name="",
            trigger_type="cancellation",
            exit_reason="too_expensive",
            rejoin_url="",
            created_at=int(time.time()) - 3 * 86400,
        ))
        resp = await h._dispatch_reactivation({
            "user": {"email": "disabled@example.com", "id": "user_222"},
        })
        assert resp.status == 200
        sends = await db.due_sends(now=int(time.time()) + 30 * 86400)
        survey_sends = [s for s in sends if s.sequence == "post_retention"]
        assert survey_sends == []
        await db.close()


class TestPostRetentionTemplate:
    """The renderer must produce a valid email + carry the survey URL
    from sub.rejoin_url through to the body and CTA."""

    def test_renders_with_survey_url(self):
        sub = Subscriber(
            email="t@example.com",
            name="Tester",
            trigger_type="post_retention",
            exit_reason="too_expensive",
            rejoin_url="https://forms.gle/abc123",
            created_at=int(time.time()),
        )
        rendered = render(
            sequence="post_retention", day=0, subscriber=sub, stats=_make_stats(),
        )
        assert "Tester" in rendered.text
        assert "https://forms.gle/abc123" in rendered.text
        assert "https://forms.gle/abc123" in rendered.html
        assert "what made you stay" in rendered.subject.lower()

    def test_falls_back_to_rejoin_url_when_no_survey_set(self):
        sub = Subscriber(
            email="t@example.com",
            name="",
            trigger_type="post_retention",
            exit_reason="other",
            rejoin_url="",
            created_at=int(time.time()),
        )
        rendered = render(
            sequence="post_retention", day=0, subscriber=sub, stats=_make_stats(),
        )
        # Falls back to the generic Potion URL instead of crashing
        assert "https://whop.com/potion" in rendered.text
