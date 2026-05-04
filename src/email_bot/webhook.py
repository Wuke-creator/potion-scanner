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

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from aiohttp import web

from src.email_bot.db import Subscriber
from src.email_bot.events_db import KNOWN_EVENT_TYPES, EmailEventsDB

logger = logging.getLogger(__name__)


# Replay-protection window for Svix signed messages. Resend's docs
# recommend rejecting anything older than 5 minutes.
_SVIX_TOLERANCE_SECONDS = 5 * 60


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
    """Verify a Whop webhook signature using the LEGACY hex-HMAC format.

    Older Whop webhooks (pre-Svix migration) sent a single ``Whop-Signature``
    header containing a hex-encoded HMAC-SHA256 of the raw body. New Whop
    webhooks use the Svix scheme (``webhook-id`` + ``webhook-timestamp`` +
    ``webhook-signature`` with a v1,base64 entry) which is verified by
    ``_whop_signature_ok_svix`` below.

    The unified ``/webhook/whop`` dispatcher tries the Svix verifier first
    and falls back to this one so endpoints configured under either format
    continue working through the migration window.
    """
    if not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, received_sig.strip().lower())


def _svix_signature_ok(
    raw_body: bytes,
    headers: Mapping[str, str],
    secret: str,
    *,
    header_prefix: str,
    now: int | None = None,
) -> bool:
    """Generic Svix-format signature verifier.

    Both Whop and Resend deliver webhooks via Svix-compatible signing,
    differing only in the header prefix:

      Whop:   ``webhook-id`` / ``webhook-timestamp`` / ``webhook-signature``
      Resend: ``svix-id``    / ``svix-timestamp``    / ``svix-signature``

    Pass the prefix without the ``-id`` / ``-timestamp`` / ``-signature``
    suffix and this function does the rest.

    The signed payload is exactly ``f"{id}.{timestamp}.{body}"`` with
    HMAC-SHA256 keyed on ``base64decode(secret_after_whsec_prefix)``,
    then base64-encoded. The header carries ``v1,<base64>`` entries
    (space-separated for key rotation); a single match anywhere is enough.
    """
    if not secret:
        return False

    msg_id = _header(headers, f"{header_prefix}-id")
    msg_ts = _header(headers, f"{header_prefix}-timestamp")
    msg_sig = _header(headers, f"{header_prefix}-signature")
    if not msg_id or not msg_ts or not msg_sig:
        return False

    try:
        ts_int = int(msg_ts)
    except ValueError:
        return False
    now_int = now if now is not None else int(time.time())
    if abs(now_int - ts_int) > _SVIX_TOLERANCE_SECONDS:
        return False

    raw_secret = secret
    if raw_secret.startswith("whsec_"):
        raw_secret = raw_secret[len("whsec_"):]
    try:
        secret_bytes = base64.b64decode(raw_secret)
    except (ValueError, TypeError):
        return False

    signed = f"{msg_id}.{msg_ts}.".encode("utf-8") + raw_body
    expected = base64.b64encode(
        hmac.new(secret_bytes, signed, hashlib.sha256).digest(),
    ).decode("ascii")

    for entry in msg_sig.split():
        if "," not in entry:
            continue
        version, _, candidate = entry.partition(",")
        if version != "v1":
            continue
        if hmac.compare_digest(expected, candidate):
            return True
    return False


def _whop_signature_ok_svix(
    raw_body: bytes, headers: Mapping[str, str], secret: str,
    *, now: int | None = None,
) -> bool:
    """Svix-format Whop signature check (current format as of 2026)."""
    return _svix_signature_ok(
        raw_body, headers, secret, header_prefix="webhook", now=now,
    )


def _resend_signature_ok(
    raw_body: bytes,
    headers: Mapping[str, str],
    secret: str,
    *,
    now: int | None = None,
) -> bool:
    """Verify a Resend webhook signature (Svix format with svix-* headers)."""
    return _svix_signature_ok(
        raw_body, headers, secret, header_prefix="svix", now=now,
    )


def _header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive single-value header lookup. Works with both
    aiohttp's CIMultiDict (where .get is already case-insensitive) and
    a plain dict (used in unit tests)."""
    val = headers.get(name)
    if val is not None:
        return val.strip() if isinstance(val, str) else ""
    lowered = name.lower()
    for k, v in headers.items():
        if isinstance(k, str) and k.lower() == lowered:
            return v.strip() if isinstance(v, str) else ""
    return ""


@dataclass
class _ParsedResendEvent:
    event_type: str       # one of KNOWN_EVENT_TYPES
    email_id: str
    recipient: str        # lowercased
    broadcast_id: str     # '' for transactional sends
    event_at: int         # epoch seconds
    click_url: str | None
    bounce_type: str | None    # 'hard' | 'soft' | 'undetermined' | None
    bounce_message: str | None


def _parse_resend_event(payload: dict) -> _ParsedResendEvent | None:
    """Pull the fields we care about out of a Resend webhook payload.

    Returns None for malformed payloads (missing email_id / recipient /
    unknown event type). The handler logs and returns 200 on None so
    Resend stops retrying a payload we'll never accept.
    """
    if not isinstance(payload, dict):
        return None
    raw_type = str(payload.get("type") or "").strip()
    if not raw_type.startswith("email."):
        return None
    event_type = raw_type[len("email."):]
    if event_type not in KNOWN_EVENT_TYPES:
        return None

    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return None

    email_id = str(data.get("email_id") or "").strip()
    if not email_id:
        return None

    to = data.get("to")
    recipient = ""
    if isinstance(to, list) and to:
        first = to[0]
        if isinstance(first, str):
            recipient = first.strip().lower()
    elif isinstance(to, str):
        recipient = to.strip().lower()
    if not recipient:
        return None

    broadcast_id = str(data.get("broadcast_id") or "").strip()
    event_at = _parse_iso8601(payload.get("created_at"))

    click_url = None
    click = data.get("click")
    if isinstance(click, dict):
        link = click.get("link")
        if isinstance(link, str) and link:
            click_url = link

    bounce_type = None
    bounce_message = None
    bounce = data.get("bounce")
    if isinstance(bounce, dict):
        bt = bounce.get("type")
        if isinstance(bt, str) and bt:
            bounce_type = bt.strip().lower()
        bm = bounce.get("message")
        if isinstance(bm, str) and bm:
            bounce_message = bm[:1000]

    return _ParsedResendEvent(
        event_type=event_type,
        email_id=email_id,
        recipient=recipient,
        broadcast_id=broadcast_id,
        event_at=event_at,
        click_url=click_url,
        bounce_type=bounce_type,
        bounce_message=bounce_message,
    )


def _parse_iso8601(value) -> int:
    """Best-effort ISO 8601 -> epoch seconds. Falls back to 'now' if
    unparseable so we never reject an event for a timestamp glitch."""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value:
        # Resend sends a trailing 'Z'; fromisoformat() doesn't accept it
        # before Python 3.11. Normalise to '+00:00' to be safe.
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return int(datetime.fromisoformat(s).timestamp())
        except ValueError:
            pass
    return int(time.time())


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
        whop_members_db=None,
        resend_webhook_secret: str = "",
        events_db: EmailEventsDB | None = None,
        save_offer_router=None,
    ):
        """``whop_members_db`` is optional. When provided, the new
        payment_failed and payment_succeeded webhook receivers can flip
        the dunning_active flag on the member's row so the
        DunningSequence cron picks them up. Without it those endpoints
        return 503 (server didn't start with members-DB wiring).

        ``resend_webhook_secret`` and ``events_db`` enable the Resend
        webhook receiver. When either is missing, /webhook/resend still
        mounts but rejects requests (signature verification fails on
        empty secret; events_db missing logs an error and 200s so
        Resend doesn't retry forever).
        """
        self._db = db
        self._whop_secret = whop_webhook_secret
        self._admin_secret = admin_secret
        self._default_rejoin = rejoin_url_default
        self._whop_members_db = whop_members_db
        self._resend_secret = resend_webhook_secret
        self._events_db = events_db
        self._save_offer_router = save_offer_router

    def register(self, app: web.Application) -> None:
        # Unified Whop dispatcher (recommended). Configure ONE webhook in
        # Whop pointing at /webhook/whop and subscribe to all events you
        # care about; this handler dispatches by the ``event`` field.
        app.router.add_post("/webhook/whop", self._whop_dispatcher)
        # Per-event endpoints kept for backward compatibility with any
        # existing Whop webhook configs that point at the older URLs.
        app.router.add_post(
            "/webhook/whop/cancellation", self._whop_cancellation,
        )
        app.router.add_post(
            "/webhook/whop/payment-failed", self._whop_payment_failed,
        )
        app.router.add_post(
            "/webhook/whop/payment-succeeded", self._whop_payment_succeeded,
        )
        app.router.add_post("/webhook/inactivity", self._inactivity)
        app.router.add_post("/webhook/resend", self._resend_webhook)
        app.router.add_post("/admin/email/test", self._admin_test)
        app.router.add_get("/admin/email/status", self._admin_status)

    # -----------------------------------------------------------------

    def _check_whop_signature(
        self, raw_body: bytes, headers: Mapping[str, str],
    ) -> bool:
        """Verify a Whop webhook signature using either the current
        Svix-format scheme (``webhook-id`` / ``webhook-timestamp`` /
        ``webhook-signature``) or the legacy single ``Whop-Signature``
        hex-HMAC scheme.

        We try Svix first because that's the format Whop actually ships
        today. The legacy check stays as a fallback so any pre-existing
        webhook config configured before the platform-side migration
        keeps working.
        """
        if _whop_signature_ok_svix(raw_body, headers, self._whop_secret):
            return True
        legacy_sig = _header(headers, "Whop-Signature")
        if legacy_sig and _whop_signature_ok(
            raw_body, self._whop_secret, legacy_sig,
        ):
            return True
        return False

    async def _whop_dispatcher(self, request: web.Request) -> web.Response:
        """Single Whop webhook endpoint that dispatches by ``event``.

        Whop sends one URL all events configured for an endpoint, with a
        body shaped ``{"event": "membership.deactivated", "data": {...}}``.
        Configure ONE endpoint in your Whop dashboard pointing here,
        subscribe to whichever events you want this bot to handle, and
        dispatch happens automatically.

        Currently routed:
          - ``membership.deactivated`` → cancellation handler (winback enrol)
          - ``membership.activated`` → clear dunning + log reactivation
          - ``payment.failed`` → start dunning cycle
          - ``payment.succeeded`` → clear dunning cycle
          - any other event → log + 200 OK (so Whop stops retrying)
        """
        raw, data = await _read_json(request)
        if not self._check_whop_signature(raw, request.headers):
            logger.warning(
                "Whop webhook rejected: bad or missing signature "
                "(svix headers present=%s, legacy header present=%s)",
                bool(_header(request.headers, "webhook-id")),
                bool(_header(request.headers, "Whop-Signature")),
            )
            return web.json_response({"error": "bad signature"}, status=401)

        event = str(data.get("event") or data.get("type") or "").strip()
        if not event:
            logger.warning("Whop dispatcher: payload has no event field")
            return web.json_response({"error": "missing event"}, status=400)

        # The event payload nests details under "data" by Whop's convention.
        # Inner handlers call _read_json themselves; we instead pass the
        # parsed inner data through a tiny shim that forwards to the
        # existing per-event implementations.
        inner = data.get("data") if isinstance(data.get("data"), dict) else data

        if event == "membership.deactivated":
            return await self._dispatch_cancellation(inner)
        if event == "membership.activated":
            return await self._dispatch_reactivation(inner)
        if event == "payment.failed":
            return await self._dispatch_payment_failed(inner)
        if event == "payment.succeeded":
            return await self._dispatch_payment_succeeded(inner)

        logger.info(
            "Whop dispatcher: ignoring unhandled event %s", event,
        )
        return web.json_response({"ok": True, "event": event, "handled": False})

    async def _dispatch_cancellation(self, data: dict) -> web.Response:
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
        # Prefer Whop's structured cancel_option (verified per docs) over the
        # free-text cancellation_reason. Both can co-exist on the payload.
        reason_raw = (
            data.get("cancel_option")
            or data.get("cancellation_reason")
            or data.get("reason")
            or (data.get("survey") or {}).get("reason", "")
        )
        if not email:
            return web.json_response({"error": "missing email"}, status=400)
        reason = normalize_reason(reason_raw)

        # AUT-026 Targeted Save Offer: fire BEFORE the winback sequence so
        # the save-offer email lands first (within ~60s of cancellation).
        # Failures here MUST NOT block winback enrolment — the save offer
        # is best-effort, the winback is the safety net.
        discord_user_id = self._extract_whop_user_id(data) or str(
            (data.get("user") or {}).get("discord_id", "")
        ).strip()
        save_outcome = "skipped"
        if self._save_offer_router is not None:
            try:
                result = await self._save_offer_router.route_offer(
                    email=email,
                    name=name,
                    reason=reason,
                    discord_user_id=discord_user_id,
                )
                save_outcome = (
                    f"routed:{result.variant_label}" if result.routed
                    else "no_variant"
                )
            except Exception:
                logger.exception(
                    "SaveOfferRouter.route_offer crashed for %s "
                    "(reason=%s); falling through to winback only",
                    email, reason,
                )

        await self._enroll(
            email=email, name=name, trigger="cancellation", reason=reason,
        )
        logger.info(
            "Whop membership.deactivated: %s reason=%s save_offer=%s "
            "winback_enrolled=true",
            email, reason, save_outcome,
        )
        return web.json_response({
            "ok": True,
            "sequence": "winback",
            "save_offer": save_outcome,
        })

    async def _dispatch_reactivation(self, data: dict) -> web.Response:
        """membership.activated fires both for first-ever joins and for
        churned members coming back. We treat both identically: stop any
        active dunning so the member doesn't keep getting reminders, and
        log so the (future) AUT-030 Welcome Back automation can fire."""
        if self._whop_members_db is None:
            return web.json_response({"ok": True, "noted": False})
        whop_user_id = self._extract_whop_user_id(data)
        if whop_user_id:
            try:
                await self._whop_members_db.stop_dunning(whop_user_id)
            except Exception:
                logger.exception(
                    "membership.activated: stop_dunning crashed for %s",
                    whop_user_id,
                )
        logger.info(
            "Whop membership.activated: %s reactivated (dunning cleared)",
            whop_user_id or "unknown",
        )
        return web.json_response({"ok": True, "user_id": whop_user_id})

    async def _dispatch_payment_failed(self, data: dict) -> web.Response:
        if self._whop_members_db is None:
            return web.json_response(
                {"error": "whop_members_db not wired"}, status=503,
            )
        whop_user_id = self._extract_whop_user_id(data)
        if not whop_user_id:
            return web.json_response(
                {"error": "missing whop_user_id"}, status=400,
            )
        try:
            started = await self._whop_members_db.start_dunning(whop_user_id)
        except Exception:
            logger.exception(
                "payment.failed dispatch: start_dunning crashed for %s",
                whop_user_id,
            )
            return web.json_response({"error": "internal"}, status=500)
        logger.info(
            "Whop payment.failed (via dispatcher): %s dunning_active=%s",
            whop_user_id, started,
        )
        return web.json_response({"ok": True, "started_cycle": started})

    async def _dispatch_payment_succeeded(self, data: dict) -> web.Response:
        if self._whop_members_db is None:
            return web.json_response(
                {"error": "whop_members_db not wired"}, status=503,
            )
        whop_user_id = self._extract_whop_user_id(data)
        if not whop_user_id:
            return web.json_response(
                {"error": "missing whop_user_id"}, status=400,
            )
        try:
            await self._whop_members_db.stop_dunning(whop_user_id)
        except Exception:
            logger.exception(
                "payment.succeeded dispatch: stop_dunning crashed for %s",
                whop_user_id,
            )
            return web.json_response({"error": "internal"}, status=500)
        logger.info(
            "Whop payment.succeeded (via dispatcher): %s dunning cleared",
            whop_user_id,
        )
        return web.json_response({"ok": True})

    # -----------------------------------------------------------------

    async def _whop_cancellation(self, request: web.Request) -> web.Response:
        """Legacy single-event endpoint. Delegates to the same dispatch
        logic as the unified /webhook/whop endpoint so save offers fire
        here too."""
        raw, data = await _read_json(request)
        if not self._check_whop_signature(raw, request.headers):
            logger.warning("Whop webhook rejected: bad or missing signature")
            return web.json_response({"error": "bad signature"}, status=401)
        # The legacy endpoint receives the membership.deactivated payload
        # at the top level (no event/data wrapper), so pass `data` as the
        # inner payload directly.
        return await self._dispatch_cancellation(data)
    async def _whop_payment_failed(self, request: web.Request) -> web.Response:
        """Whop fires this when a member's auto-renewal charge fails.

        Sets dunning_active=1 on the member's whop_members row. The
        DunningSequence cron picks it up on its next pass and starts the
        Day 0 / 3 / 10 email sequence.
        """
        raw, data = await _read_json(request)
        if not self._check_whop_signature(raw, request.headers):
            logger.warning(
                "Whop payment_failed webhook rejected: bad signature",
            )
            return web.json_response({"error": "bad signature"}, status=401)

        if self._whop_members_db is None:
            return web.json_response(
                {"error": "whop_members_db not wired"}, status=503,
            )

        whop_user_id = self._extract_whop_user_id(data)
        if not whop_user_id:
            return web.json_response(
                {"error": "missing whop_user_id"}, status=400,
            )
        try:
            started = await self._whop_members_db.start_dunning(whop_user_id)
        except Exception:
            logger.exception(
                "payment_failed: start_dunning crashed for %s", whop_user_id,
            )
            return web.json_response({"error": "internal"}, status=500)
        logger.info(
            "Whop payment_failed: %s dunning_active=%s",
            whop_user_id, started,
        )
        return web.json_response({"ok": True, "started_cycle": started})

    async def _whop_payment_succeeded(self, request: web.Request) -> web.Response:
        """Whop fires this when a member's payment goes through after a
        retry (or just normally). If they were in a dunning cycle, end it."""
        raw, data = await _read_json(request)
        if not self._check_whop_signature(raw, request.headers):
            logger.warning(
                "Whop payment_succeeded webhook rejected: bad signature",
            )
            return web.json_response({"error": "bad signature"}, status=401)

        if self._whop_members_db is None:
            return web.json_response(
                {"error": "whop_members_db not wired"}, status=503,
            )

        whop_user_id = self._extract_whop_user_id(data)
        if not whop_user_id:
            return web.json_response(
                {"error": "missing whop_user_id"}, status=400,
            )
        try:
            await self._whop_members_db.stop_dunning(whop_user_id)
        except Exception:
            logger.exception(
                "payment_succeeded: stop_dunning crashed for %s",
                whop_user_id,
            )
            return web.json_response({"error": "internal"}, status=500)
        logger.info(
            "Whop payment_succeeded: %s dunning cleared", whop_user_id,
        )
        return web.json_response({"ok": True})

    async def _resend_webhook(self, request: web.Request) -> web.Response:
        """Resend webhook receiver. Records the event for stats and runs
        auto-suppression on hard bounces and complaints (and on the third
        soft bounce within 30 days for the same recipient).

        Idempotent: the email_events table has a UNIQUE constraint on
        (resend_email_id, event_type, event_at) so Resend retrying on
        a non-200 we already processed produces zero new rows and zero
        additional suppression actions.
        """
        raw, data = await _read_json(request)
        if not _resend_signature_ok(raw, request.headers, self._resend_secret):
            logger.warning("Resend webhook rejected: bad or missing signature")
            return web.json_response({"error": "bad signature"}, status=401)

        if self._events_db is None:
            # Don't 500 — Resend retries forever on non-200. Log and ack.
            logger.error("Resend webhook received but events_db not wired")
            return web.json_response({"ok": True, "stored": False})

        event = _parse_resend_event(data)
        if event is None:
            logger.warning(
                "Resend webhook: unparseable event %s", str(data)[:200],
            )
            return web.json_response({"ok": True, "stored": False})

        try:
            inserted = await self._events_db.record_event(
                resend_email_id=event.email_id,
                broadcast_id=event.broadcast_id,
                recipient=event.recipient,
                event_type=event.event_type,
                event_at=event.event_at,
                click_url=event.click_url,
                bounce_type=event.bounce_type,
                bounce_message=event.bounce_message,
                raw_payload=json.dumps(data, separators=(",", ":")),
            )
        except Exception:
            logger.exception(
                "Resend webhook: record_event crashed for %s/%s",
                event.email_id, event.event_type,
            )
            # Returning 500 lets Resend retry, which is the right call when
            # our DB is sick because the retry will succeed once we recover.
            return web.json_response({"error": "record failed"}, status=500)

        if inserted and self._whop_members_db is not None and event.recipient:
            try:
                await self._maybe_suppress(event)
            except Exception:
                # Suppression is best-effort. Log loudly but don't ask
                # Resend to retry — the event is already recorded and
                # we'd just keep looping on the same failure.
                logger.exception(
                    "Resend webhook: suppression check crashed for %s",
                    event.recipient,
                )

        logger.info(
            "Resend webhook %s for %s (broadcast=%s, inserted=%s)",
            event.event_type, event.recipient, event.broadcast_id or "-",
            inserted,
        )
        return web.json_response({"ok": True, "stored": inserted})

    async def _maybe_suppress(self, event: _ParsedResendEvent) -> None:
        """Apply the auto-suppression rules described in the plan:

          - complained -> immediate suppression
          - bounced + bounce_type='hard' -> immediate suppression
          - bounced + bounce_type='soft' -> suppress when count >= 3
            in the last 30 days

        ``event`` is assumed to have just been newly inserted (caller
        checks the inserted flag), so we only run for first-delivery
        events. The whop_members_db lookup is case-insensitive exact
        match on email and may flip multiple rows (the same person can
        hold multiple memberships under different whop_user_ids).
        """
        if event.event_type == "complained":
            n = await self._whop_members_db.mark_invalid_by_email(
                event.recipient,
            )
            if n > 0:
                logger.info(
                    "Suppressed %d whop_members row(s) for complaint from %s",
                    n, event.recipient,
                )
            return

        if event.event_type != "bounced":
            return

        if event.bounce_type == "hard":
            n = await self._whop_members_db.mark_invalid_by_email(
                event.recipient,
            )
            if n > 0:
                logger.info(
                    "Suppressed %d whop_members row(s) for hard bounce of %s",
                    n, event.recipient,
                )
            return

        if event.bounce_type == "soft":
            cutoff = int(time.time()) - 30 * 86400
            count = await self._events_db.soft_bounce_count(
                event.recipient, since_epoch=cutoff,
            )
            if count >= 3:
                n = await self._whop_members_db.mark_invalid_by_email(
                    event.recipient,
                )
                if n > 0:
                    logger.info(
                        "Suppressed %d whop_members row(s) after %d soft "
                        "bounces in 30 days for %s",
                        n, count, event.recipient,
                    )

    @staticmethod
    def _extract_whop_user_id(data: dict) -> str:
        """Pull the Whop user ID out of the (variable-shape) webhook body."""
        candidates = (
            data.get("whop_user_id"),
            data.get("user_id"),
            (data.get("user") or {}).get("id"),
            (data.get("membership") or {}).get("user_id"),
            (data.get("membership") or {}).get("user", {}).get("id"),
        )
        for c in candidates:
            if c:
                return str(c)
        return ""

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
