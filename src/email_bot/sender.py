"""Resend HTTP client for sending emails.

Resend was chosen because:
  - Simplest API (one POST to /emails with JSON)
  - Free tier: 3,000 sends/month, 100/day (plenty for win-back at current scale)
  - Handles both HTML + plain text fallback in one call
  - Good deliverability out of the box with DKIM/SPF
  - No SDK required, stdlib POST works fine (but we use aiohttp since
    we're already async everywhere)

To use:
  1. Sign up at https://resend.com
  2. Verify a sending domain (or use onboarding@resend.dev for testing)
  3. Get an API key, put it in RESEND_API_KEY
  4. Set RESEND_FROM_ADDRESS to something like 'Potion <team@yourdomain.com>'

Sends never raise; they return a ``SendResult`` with success/error so the
worker can decide what to do (retry vs mark-failed).

When ``unsub_secret`` and ``public_base_url`` are set, every send:
  1. Substitutes the ``{{{RESEND_UNSUBSCRIBE_URL}}}`` macro in the body
     with a per-recipient signed URL pointing at our /unsubscribe handler
  2. Adds the RFC-8058 ``List-Unsubscribe`` and ``List-Unsubscribe-Post``
     headers so Gmail / Yahoo render the native one-click unsub button
     (mandatory bulk-sender requirement since Feb 2024).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

from src.email_bot.webhook import compute_unsub_token

logger = logging.getLogger(__name__)


_UNSUB_MACRO = "{{{RESEND_UNSUBSCRIBE_URL}}}"


@dataclass
class SendResult:
    ok: bool
    resend_id: str | None = None
    error: str | None = None


class ResendClient:
    """Async client for Resend's /emails endpoint."""

    API_URL = "https://api.resend.com/emails"
    BROADCASTS_URL = "https://api.resend.com/broadcasts"

    def __init__(
        self,
        api_key: str,
        from_address: str,
        session: aiohttp.ClientSession | None = None,
        timeout_sec: float = 15.0,
        unsub_secret: str = "",
        public_base_url: str = "",
    ):
        self._api_key = api_key
        self._from = from_address
        self._owns_session = session is None
        self._session = session
        self._timeout = timeout_sec
        self._unsub_secret = unsub_secret
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else ""

    async def __aenter__(self) -> "ResendClient":
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def send(
        self,
        to: str,
        subject: str,
        html: str,
        text: str,
        from_name: str | None = None,
        reply_to: str | None = None,
        unsub_source: str = "",
    ) -> SendResult:
        """Send one email via Resend. Never raises."""
        if self._session is None:
            self._session = aiohttp.ClientSession()

        # Resend accepts "Name <addr@domain>" in the 'from' field.
        from_field = self._from
        if from_name and "<" not in from_field:
            from_field = f"{from_name} <{self._from}>"

        unsub_url = self._unsub_url_for(to, unsub_source)
        if unsub_url:
            html = html.replace(_UNSUB_MACRO, unsub_url) if html else html
            text = text.replace(_UNSUB_MACRO, unsub_url) if text else text

        payload: dict = {
            "from": from_field,
            "to": [to],
            "subject": subject,
            "html": html,
            "text": text,
        }
        if reply_to:
            payload["reply_to"] = reply_to
        if unsub_url:
            # RFC 8058 one-click unsubscribe headers — required by Gmail and
            # Yahoo for any sender doing bulk volume. The mailbox provider
            # POSTs to the URL with the literal body
            # 'List-Unsubscribe=One-Click' when the user clicks the native
            # button next to the sender name.
            payload["headers"] = {
                "List-Unsubscribe": f"<{unsub_url}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            }

        try:
            async with self._session.post(
                self.API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    logger.warning(
                        "Resend API returned %d for %s: %s",
                        resp.status, to, body[:200],
                    )
                    return SendResult(
                        ok=False,
                        error=f"HTTP {resp.status}: {body[:200]}",
                    )
                try:
                    data = await resp.json(content_type=None)
                    resend_id = data.get("id") if isinstance(data, dict) else None
                except Exception:
                    resend_id = None
                return SendResult(ok=True, resend_id=resend_id)
        except aiohttp.ClientError as e:
            logger.warning("Resend transport error for %s: %s", to, e)
            return SendResult(ok=False, error=f"network: {e}")
        except Exception as e:
            logger.exception("Unexpected Resend error for %s", to)
            return SendResult(ok=False, error=f"unexpected: {e}")

    async def get_broadcast(self, broadcast_id: str) -> dict | None:
        """Read-only lookup of one Resend broadcast's metadata.

        Used by the /email-broadcast-stats Discord slash command to print
        the broadcast's display name alongside its ID. Returns None on
        404 (unknown ID), network error, or empty broadcast_id so the
        caller can fall back to a placeholder without try/except.
        """
        if not broadcast_id:
            return None
        if self._session is None:
            self._session = aiohttp.ClientSession()
        url = f"{self.BROADCASTS_URL}/{broadcast_id}"
        try:
            async with self._session.get(
                url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as resp:
                if resp.status != 200:
                    return None
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    return None
                return data if isinstance(data, dict) else None
        except aiohttp.ClientError as e:
            logger.warning("Resend get_broadcast transport error: %s", e)
            return None
        except Exception:
            logger.exception("Unexpected Resend get_broadcast error")
            return None

    def _unsub_url_for(self, recipient: str, source: str) -> str:
        """Build the per-recipient unsubscribe URL.

        Returns "" when ``unsub_secret`` or ``public_base_url`` is not
        configured, so the macro stays unsubstituted (Resend treats it as
        plain text in transactional sends; harmless).
        """
        if not (self._unsub_secret and self._public_base_url and recipient):
            return ""
        token = compute_unsub_token(self._unsub_secret, recipient)
        if not token:
            return ""
        url = (
            f"{self._public_base_url}/unsubscribe"
            f"?e={quote(recipient.lower(), safe='')}"
            f"&t={token}"
        )
        if source:
            url += f"&s={quote(source, safe='')}"
        return url
