"""Regression tests: Whop sends event names in either dot or underscore
format depending on which webhook version was used to configure the
endpoint. Nourek configured Potion's endpoint 2026-05-10 with underscore
events (``payment_succeeded``, ``payment_failed``, ``membership_activated``,
``membership_deactivated``).

The dispatcher must accept both forms.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web

from src.email_bot.db import EmailDB
from src.email_bot.webhook import EmailWebhookHandlers


@pytest.mark.asyncio
class TestWhopEventNameNormalisation:
    async def _make_handlers(self, tmp_path) -> EmailWebhookHandlers:
        db = EmailDB(db_path=str(tmp_path / "wh.db"))
        await db.open()
        members_db = AsyncMock()
        # Both flips return without complaining; we only care which was called.
        members_db.start_dunning = AsyncMock(return_value=True)
        members_db.stop_dunning = AsyncMock(return_value=None)
        return EmailWebhookHandlers(
            db=db,
            whop_webhook_secret="x",
            admin_secret="y",
            rejoin_url_default="https://whop.com/potion",
            whop_members_db=members_db,
        )

    async def _post(
        self, h: EmailWebhookHandlers, body: dict,
    ) -> web.Response:
        """Fire ``_whop_dispatcher`` with the signature check stubbed
        so we exercise only the event-routing logic."""
        raw = json.dumps(body).encode()
        req = AsyncMock()
        req.read = AsyncMock(return_value=raw)
        req.headers = {}
        with patch.object(
            h, "_check_whop_signature", return_value=True,
        ):
            return await h._whop_dispatcher(req)

    @pytest.mark.parametrize("event_name", [
        "payment.failed",
        "payment_failed",
        "Payment_Failed",
        "PAYMENT_FAILED",
    ])
    async def test_payment_failed_event_variants_all_route_to_dunning(
        self, tmp_path, event_name,
    ):
        h = await self._make_handlers(tmp_path)
        with patch.object(
            h, "_dispatch_payment_failed",
            new=AsyncMock(return_value=web.json_response({"ok": True})),
        ) as mocked:
            await self._post(h, {
                "event": event_name,
                "data": {"whop_user_id": "user_x"},
            })
            mocked.assert_awaited_once()

    @pytest.mark.parametrize("event_name", [
        "payment.succeeded", "payment_succeeded",
    ])
    async def test_payment_succeeded_variants_clear_dunning(
        self, tmp_path, event_name,
    ):
        h = await self._make_handlers(tmp_path)
        with patch.object(
            h, "_dispatch_payment_succeeded",
            new=AsyncMock(return_value=web.json_response({"ok": True})),
        ) as mocked:
            await self._post(h, {
                "event": event_name,
                "data": {"whop_user_id": "user_x"},
            })
            mocked.assert_awaited_once()

    @pytest.mark.parametrize("event_name", [
        "membership.deactivated", "membership_deactivated",
    ])
    async def test_membership_deactivated_variants_route_to_cancellation(
        self, tmp_path, event_name,
    ):
        h = await self._make_handlers(tmp_path)
        with patch.object(
            h, "_dispatch_cancellation",
            new=AsyncMock(return_value=web.json_response({"ok": True})),
        ) as mocked:
            await self._post(h, {
                "event": event_name,
                "data": {
                    "user": {"email": "x@example.com"},
                    "cancellation_reason": "too_expensive",
                },
            })
            mocked.assert_awaited_once()

    @pytest.mark.parametrize("event_name", [
        "membership.activated", "membership_activated",
    ])
    async def test_membership_activated_variants_route_to_reactivation(
        self, tmp_path, event_name,
    ):
        h = await self._make_handlers(tmp_path)
        with patch.object(
            h, "_dispatch_reactivation",
            new=AsyncMock(return_value=web.json_response({"ok": True})),
        ) as mocked:
            await self._post(h, {
                "event": event_name,
                "data": {"user": {"email": "x@example.com"}},
            })
            mocked.assert_awaited_once()

    async def test_unknown_event_logs_and_returns_200(self, tmp_path):
        """Whop can subscribe arbitrary events to the endpoint. We ack
        with 200 and a ``handled: false`` flag rather than retrying or
        erroring, so Whop's retry policy doesn't spin on unknown events.
        """
        h = await self._make_handlers(tmp_path)
        resp = await self._post(h, {
            "event": "some.weird.event_we_dont_handle_yet",
            "data": {},
        })
        assert resp.status == 200
        body = json.loads(resp.body.decode() if isinstance(resp.body, bytes) else resp.body)
        assert body.get("handled") is False

    async def test_missing_event_field_returns_400(self, tmp_path):
        h = await self._make_handlers(tmp_path)
        resp = await self._post(h, {"data": {}})
        assert resp.status == 400
