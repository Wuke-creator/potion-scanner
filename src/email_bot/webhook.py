"""aiohttp route handlers for the email bot.

Three routes, mounted on the shared aiohttp app (same one hosting the
Discord OAuth callback on port 8080):

  POST /webhook/whop/cancellation
    Whop fires this when a member cancels. Signature-verified. Enrols
    the user in the 4-email win-back sequence.

  POST /webhook/inactivity
    Generic inactivity trigger. Internal caller (cron/monitor) posts a
    shared-secret header + payload to enrol a user in the re-engagement
    sequence.

  POST /admin/email/test
    Shared-secret endpoint for manual testing. Takes {email, name,
    sequence, day, exit_reason?} and immediately queues OR renders a
    single email without scheduling a full sequence.

Signature verification uses ``WHOP_WEBHOOK_SECRET`` for Whop (HMAC-SHA256
over the raw body) and ``ADMIN_WEBHOOK_SECRET`` for the internal routes
(shared secret in ``X-Admin-Secret`` header).

Exit reason mapping from Whop payload:
  The Whop Cancel Membership app exit survey sends back a free-text
  ``cancellation_reason`` field. We normalize common strings into the
  EXIT_REASONS codes used by the template's Offer A-F logic.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

from aiohttp import web

from src.email_bot.db import Subscriber

logger = logging.getLogger(__name__)


# Map the raw survey options from the spec (05_Survey_Feedback) to our
# short codes. Anything unrecognized falls back to 'other' which renders
# Offer F.
_REASON_ALIASES = {
    "market_slow": "market_slow",
    "market is slow / taking a break": "market_slow",
    "market slow": "market_slow",
    "taking a break": "market_slow",
    "not_using": "not_using",
    "not using it enough": "not_using",
    "not using": "not_using",
    "too_expensive": "too_expensive",
    "too expensive": "too_expensive",
    "quality_declined": "quality_declined",
    "quality of calls declined": "quality_declined",
    "quality declined": "quality_declined",
    "found_alternative": "found_alternative",
    "found a better alternative": "found_alternative",
    "fulfillment": "fulfillment",
    "fulfillment issue": "fulfillment",
    "other": "other",
}


def normalize_reason(raw: str | None) -> str:
    if not raw:
        return "other"
    key = raw.strip().lower()
    return _REASON_ALIASES.get(key, "other")


def _whop_signature_ok(raw_body: bytes, secret: str, received_sig: str) -> bool:
    """Verify a Whop webhook signature (HMAC-SHA256 hex digest)."""
    if not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, received_sig.strip().lower())


async def _read_json(request: web.Request) -> tuple[bytes, dict]:
    raw = await request.read()
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        data = {}
    return raw, data


class EmailWebhookHandlers:
    """Bundle of aiohttp handlers that close over the email subsystem."""

    def __init__(
        self,
        db,
        whop_webhook_secret: str,
        admin_secret: str,
        rejoin_url_default: str,
    ):
        self._db = db
        self._whop_secret = whop_webhook_secret
        self._admin_secret = admin_secret
        self._default_rejoin = rejoin_url_default

    def register(self, app: web.Application) -> None:
        app.router.add_post(
            "/webhook/whop/cancellation", self._whop_cancellation,
        )
        app.router.add_post("/webhook/inactivity", self._inactivity)
        app.router.add_post("/admin/email/test", self._admin_test)
        app.router.add_get("/admin/email/status", self._admin_status)

    # -----------------------------------------------------------------

    async def _whop_cancellation(self, request: web.Request) -> web.Response:
        raw, data = await _read_json(request)
        sig = request.headers.get("Whop-Signature", "").strip()
        if not self._whop_secret or not _whop_signature_ok(
            raw, self._whop_secret, sig,
        ):
            logger.warning("Whop webhook rejected: bad or missing signature")
            return web.json_response({"error": "bad signature"}, status=401)

        # Whop's payload shape varies; accept a few common layouts
        email = (
            data.get("email")
            or (data.get("user") or {}).get("email")
            or (data.get("membership") or {}).get("user", {}).get("email", "")
        ).strip()
        name = (
            data.get("name")
            or (data.get("user") or {}).get("name")
            or (data.get("user") or {}).get("username", "")
        ).strip()
        reason_raw = (
            data.get("cancellation_reason")
            or data.get("reason")
            or (data.get("survey") or {}).get("reason", "")
        )

        if not email:
            return web.json_response({"error": "missing email"}, status=400)

        reason = normalize_reason(reason_raw)
        await self._enroll(
            email=email, name=name, trigger="cancellation", reason=reason,
        )
        logger.info("Whop cancellation enrolled %s (reason=%s)", email, reason)
        return web.json_response({"ok": True, "sequence": "winback"})

    async def _inactivity(self, request: web.Request) -> web.Response:
        if not self._admin_check(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        _, data = await _read_json(request)
        email = (data.get("email") or "").strip()
        name = (data.get("name") or "").strip()
        if not email:
            return web.json_response({"error": "missing email"}, status=400)
        await self._enroll(
            email=email, name=name, trigger="inactivity", reason="none",
        )
        logger.info("Inactivity enrolled %s", email)
        return web.json_response({"ok": True, "sequence": "reengagement"})

    async def _admin_test(self, request: web.Request) -> web.Response:
        """Send a single email right now, for testing templates."""
        if not self._admin_check(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        _, data = await _read_json(request)
        email = (data.get("email") or "").strip()
        sequence = (data.get("sequence") or "winback").strip()
        day = int(data.get("day", 1))
        name = (data.get("name") or "").strip()
        reason = normalize_reason(data.get("exit_reason"))
        if not email:
            return web.json_response({"error": "missing email"}, status=400)
        if sequence not in ("winback", "reengagement"):
            return web.json_response({"error": "bad sequence"}, status=400)
        if day not in (1, 3, 5, 7):
            return web.json_response({"error": "day must be 1/3/5/7"}, status=400)

        sub = Subscriber(
            email=email, name=name, trigger_type="admin_test",
            exit_reason=reason, rejoin_url=self._default_rejoin,
            created_at=int(time.time()),
        )
        await self._db.upsert_subscriber(sub)
        send_id = await self._db.schedule_one(
            email=email, sequence=sequence, day=day,
            due_at=int(time.time()),
        )
        return web.json_response({
            "ok": True,
            "send_id": send_id,
            "email": email,
            "sequence": sequence,
            "day": day,
            "reason": reason,
        })

    async def _admin_status(self, request: web.Request) -> web.Response:
        if not self._admin_check(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        counts = await self._db.count_by_status()
        return web.json_response({"ok": True, "sends": counts})

    # -----------------------------------------------------------------

    def _admin_check(self, request: web.Request) -> bool:
        if not self._admin_secret:
            return False
        given = request.headers.get("X-Admin-Secret", "").strip()
        return hmac.compare_digest(given, self._admin_secret)

    async def _enroll(
        self, email: str, name: str, trigger: str, reason: str,
    ) -> None:
        sub = Subscriber(
            email=email.lower(),
            name=name,
            trigger_type=trigger,
            exit_reason=reason,
            rejoin_url=self._default_rejoin,
            created_at=int(time.time()),
        )
        await self._db.upsert_subscriber(sub)
        sequence = "winback" if trigger == "cancellation" else "reengagement"
        await self._db.schedule_sequence(email=sub.email, sequence=sequence)
