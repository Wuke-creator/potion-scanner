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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class SendResult:
    ok: bool
    resend_id: str | None = None
    error: str | None = None


class ResendClient:
    """Async client for Resend's /emails endpoint."""

    API_URL = "https://api.resend.com/emails"

    def __init__(
        self,
        api_key: str,
        from_address: str,
        session: aiohttp.ClientSession | None = None,
        timeout_sec: float = 15.0,
    ):
        self._api_key = api_key
        self._from = from_address
        self._owns_session = session is None
        self._session = session
        self._timeout = timeout_sec

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
    ) -> SendResult:
        """Send one email via Resend. Never raises."""
        if self._session is None:
            self._session = aiohttp.ClientSession()

        # Resend accepts "Name <addr@domain>" in the 'from' field.
        from_field = self._from
        if from_name and "<" not in from_field:
            from_field = f"{from_name} <{self._from}>"

        payload: dict = {
            "from": from_field,
            "to": [to],
            "subject": subject,
            "html": html,
            "text": text,
        }
        if reply_to:
            payload["reply_to"] = reply_to

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
