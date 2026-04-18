"""Async aiohttp client for the Whop v5 REST API.

Used by the email sync task to look up members' email addresses given their
Discord user ID. Potion sells Elite access through Whop; Whop is the source of
truth for member contact info. Our verified_users table is populated via
Discord OAuth and only captures email if the user verified AFTER we added the
`email` scope to the OAuth request. For users who verified before that, this
client lets us backfill emails without requiring them to re-verify.

Endpoints used:

  GET /api/v2/memberships?company_id={company_id}&status=completed
      Paginated list of memberships scoped to one Whop company. Each row has
      top-level `email`, `valid`, `status`, `user` (ID string), and nested
      `discord: {id, username, image_url}`. We iterate pages, filter
      `valid==True` client-side (the server-side `valid` param returns 500),
      and yield WhopMember.

Auth: Bearer token in the `Authorization` header. Create the key at
https://whop.com/dashboard/<company_id>/developer/api-keys with only
"Read members" and "Read member emails" permissions checked. Higher scopes
are unnecessary and should stay off.

Rate limits + page size: Whop v2 caps `per` at 50 and allows roughly 2
requests per second without 429s. We default to 0.5s sleep between pages.
Big communities have tens of thousands of historical (cancelled/expired)
memberships; we cap the walk at `max_pages` (default 500 = 25,000 most
recent memberships) since `GET memberships` orders newest-first and all
currently-paying members are in that window. Increase the cap if Potion
grows past ~10k active members.

This client is intentionally narrow. Today we only need the memberships
listing. If we ever need user lookup by ID, membership cancellation, etc.,
add methods to this class rather than calling aiohttp directly elsewhere.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class WhopMember:
    """A row out of the company memberships listing, flattened for our use.

    We carry only the fields the email sync actually needs. The raw Whop
    payload has dozens of fields (pricing, access passes, custom fields, etc.)
    that we don't touch.
    """

    user_id: str                 # Whop user id (stable across memberships)
    discord_user_id: str         # linked Discord snowflake, "" if unlinked
    email: str                   # contact email, "" if Whop doesn't surface it
    valid: bool                  # True = active paying member
    membership_id: str           # Whop membership id (one user may have many)


@dataclass
class WhopReview:
    """One row out of the company reviews listing.

    Whop's review payload is intentionally sparse: some reviewers leave a
    star rating only (no text); some leave title+description. We always get
    the stars, timestamps, and the user/product IDs.
    """

    review_id: str               # Whop review id, e.g. "rev_ZYC3dEOIYhf36u"
    user_id: str                 # Whop user id of the reviewer
    product_id: str              # which access pass / product they reviewed
    stars: int                   # 1..5
    title: str                   # can be empty
    description: str             # can be empty (star-only review)
    created_at: int              # epoch seconds


class WhopAPIError(Exception):
    """Raised for any non-2xx response from the Whop API."""


class WhopAPIClient:
    """Thin async wrapper around the Whop v5 REST API.

    Usage:

        async with WhopAPIClient(api_key, company_id) as whop:
            async for member in whop.iter_memberships():
                ...

    The client owns an internal aiohttp.ClientSession so callers just use
    context manager syntax and let __aexit__ clean up sockets.
    """

    def __init__(
        self,
        api_key: str,
        company_id: str,
        api_base: str = "https://api.whop.com",
        *,
        page_size: int = 50,
        per_page_sleep: float = 0.5,
        max_pages: int = 10000,
        timeout_seconds: float = 60.0,
        max_retries_per_page: int = 3,
    ):
        if not api_key:
            raise ValueError("api_key required")
        if not company_id:
            raise ValueError("company_id required")
        # Whop v2 caps per at 50; higher values return 500 Internal Server
        # Error, so clamp here and log a warning if caller tried higher.
        if page_size > 50:
            logger.warning(
                "Whop v2 memberships per-page cap is 50; clamping from %d",
                page_size,
            )
            page_size = 50
        self._api_key = api_key
        self._company_id = company_id
        self._api_base = api_base.rstrip("/")
        self._page_size = page_size
        self._per_page_sleep = per_page_sleep
        self._max_pages = max_pages
        self._max_retries = max_retries_per_page
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "WhopAPIClient":
        self._session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
            },
            timeout=self._timeout,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def iter_memberships(self):
        """Yield every membership in the company, one at a time.

        Walks Whop v2 memberships ordered newest-first. Stops at `max_pages`
        or sooner if the page comes back short. Sleeps `per_page_sleep`
        between pages to stay under rate limits. Yields membership rows the
        caller can filter by `valid` client-side; we don't filter here
        because the sync wants to see invalid rows too (to mark cancelled
        members inactive in our own DB).
        """
        assert self._session is not None, "use inside `async with` block"
        url = f"{self._api_base}/api/v2/memberships"
        page = 1
        total_yielded = 0
        consecutive_failures = 0
        while page <= self._max_pages:
            params = {
                "company_id": self._company_id,
                "status": "completed",
                "page": page,
                "per": self._page_size,
            }
            payload = None
            # Per-page retry loop. Whop's v2 endpoint is slow; we've seen 20s+
            # responses on deep pages. Timeout is 60s, retry up to 3 times per
            # page with exponential backoff before giving up.
            for attempt in range(self._max_retries):
                try:
                    async with self._session.get(url, params=params) as resp:
                        if resp.status == 429:
                            retry_after = float(resp.headers.get("Retry-After", "2"))
                            logger.warning(
                                "Whop rate limited, sleeping %.1fs before retry",
                                retry_after,
                            )
                            await asyncio.sleep(retry_after)
                            continue
                        if resp.status >= 500:
                            # Transient server error, retry
                            logger.warning(
                                "Whop returned %d on page %d (attempt %d), retrying",
                                resp.status, page, attempt + 1,
                            )
                            await asyncio.sleep(1.0 * (attempt + 1))
                            continue
                        if resp.status >= 400:
                            body = await resp.text()
                            raise WhopAPIError(
                                f"Whop memberships returned {resp.status}: {body[:300]}"
                            )
                        payload = await resp.json()
                        break  # success
                except asyncio.TimeoutError:
                    logger.warning(
                        "Whop timeout on page %d (attempt %d/%d)",
                        page, attempt + 1, self._max_retries,
                    )
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                except aiohttp.ClientError as e:
                    logger.warning(
                        "Whop HTTP error on page %d (attempt %d/%d): %s",
                        page, attempt + 1, self._max_retries, e,
                    )
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue

            if payload is None:
                # All retries exhausted. Rather than aborting the whole walk,
                # log, skip this page, and continue. Let consecutive_failures
                # safety abort the walk only after many consecutive failures.
                consecutive_failures += 1
                logger.error(
                    "Whop sync skipping page %d after %d failed attempts "
                    "(consecutive failures: %d)",
                    page, self._max_retries, consecutive_failures,
                )
                if consecutive_failures >= 10:
                    raise WhopAPIError(
                        f"Whop sync aborted: {consecutive_failures} consecutive "
                        f"page failures at page {page}"
                    )
                page += 1
                continue
            consecutive_failures = 0

            rows = _extract_rows(payload)
            if not rows:
                logger.info(
                    "Whop memberships walk complete: %d members across %d page(s)",
                    total_yielded, page - 1,
                )
                return

            for row in rows:
                member = _parse_member(row)
                if member is not None:
                    total_yielded += 1
                    yield member

            if len(rows) < self._page_size:
                logger.info(
                    "Whop memberships walk complete: %d members across %d page(s)",
                    total_yielded, page,
                )
                return

            page += 1
            if self._per_page_sleep > 0:
                await asyncio.sleep(self._per_page_sleep)

        logger.info(
            "Whop memberships walk stopped at max_pages=%d: %d members seen",
            self._max_pages, total_yielded,
        )

    async def iter_reviews(self, since_epoch: int = 0):
        """Yield reviews for this company, newest-first, stopping early when
        we reach reviews older than ``since_epoch``.

        Whop's reviews API at ``/api/v2/reviews?company_id=X`` returns reviews
        with `stars`, optional `title`/`description`, `user`, `product`, and
        `created_at`. This is a lot smaller dataset than memberships (hundreds
        of reviews, not hundreds of thousands), so a full walk usually finishes
        in a few seconds.

        When ``since_epoch > 0`` we stop as soon as we see a review older than
        that, which is the normal incremental-sync pattern (track last-seen
        timestamp, only yield new ones on each cycle).
        """
        assert self._session is not None, "use inside `async with` block"
        url = f"{self._api_base}/api/v2/reviews"
        page = 1
        total_yielded = 0
        while page <= self._max_pages:
            params = {
                "company_id": self._company_id,
                "page": page,
                "per": self._page_size,
            }
            payload = None
            for attempt in range(self._max_retries):
                try:
                    async with self._session.get(url, params=params) as resp:
                        if resp.status == 429:
                            retry_after = float(
                                resp.headers.get("Retry-After", "2"),
                            )
                            await asyncio.sleep(retry_after)
                            continue
                        if resp.status >= 500:
                            await asyncio.sleep(1.0 * (attempt + 1))
                            continue
                        if resp.status >= 400:
                            body = await resp.text()
                            raise WhopAPIError(
                                f"Whop reviews returned {resp.status}: {body[:300]}"
                            )
                        payload = await resp.json()
                        break
                except asyncio.TimeoutError:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                except aiohttp.ClientError as e:
                    logger.warning("Whop reviews HTTP error: %s", e)
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
            if payload is None:
                logger.error("Whop reviews page %d failed after retries", page)
                return

            rows = _extract_rows(payload)
            if not rows:
                return

            for row in rows:
                review = _parse_review(row)
                if review is None:
                    continue
                if since_epoch and review.created_at <= since_epoch:
                    # Newest-first ordering: once we hit an older one, we've
                    # already seen everything after it.
                    logger.info(
                        "Whop reviews walk stopping at page %d: hit review "
                        "older than since_epoch",
                        page,
                    )
                    return
                total_yielded += 1
                yield review

            if len(rows) < self._page_size:
                return
            page += 1
            if self._per_page_sleep > 0:
                await asyncio.sleep(self._per_page_sleep)


def _extract_rows(payload: Any) -> list[dict]:
    """Pull the list of membership rows out of a v5 response.

    Whop's response shape has shifted slightly across versions. We accept the
    most common layouts: `{"data": [...]}`, `{"memberships": [...]}`, or a
    bare list.
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "memberships", "rows", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _parse_member(row: dict) -> WhopMember | None:
    """Flatten a v2 memberships row into our narrow WhopMember dataclass.

    v2 schema puts the fields we care about at the top level:

      row = {
        "id": "mem_...",            # membership id
        "user": "user_...",          # user id (string, not nested object)
        "email": "user@example.com", # contact email
        "valid": True,               # currently paying / trialing
        "status": "completed",       # whop lifecycle status
        "discord": {"id": "8366..."} # linked Discord account, or null
      }

    Returns None only if the row has no identifiers at all, which should
    only happen on malformed payloads.
    """
    if not isinstance(row, dict):
        return None

    user_id = str(row.get("user") or row.get("user_id") or "")
    email = str(row.get("email") or "").strip().lower()

    discord_user_id = ""
    discord_field = row.get("discord")
    if isinstance(discord_field, dict):
        discord_user_id = str(discord_field.get("id") or "")
    elif isinstance(discord_field, str):
        discord_user_id = discord_field
    if not discord_user_id:
        discord_user_id = str(row.get("discord_id") or "")

    valid_value = row.get("valid")
    if valid_value is None:
        status = str(row.get("status") or "").lower()
        valid = status in {"active", "valid", "completed", "trialing", "paid"}
    else:
        valid = bool(valid_value)

    membership_id = str(row.get("id") or row.get("membership_id") or "")

    if not user_id and not discord_user_id and not email:
        return None

    return WhopMember(
        user_id=user_id,
        discord_user_id=discord_user_id,
        email=email,
        valid=valid,
        membership_id=membership_id,
    )


def _parse_review(row: dict) -> WhopReview | None:
    """Flatten a v2 reviews row into our narrow WhopReview dataclass.

    v2 schema:

      row = {
        "id": "rev_...",
        "user": "user_...",
        "product": "prod_...",
        "stars": 5,
        "title": null | str,
        "description": null | str,
        "created_at": 1772244575,
        ...
      }
    """
    if not isinstance(row, dict):
        return None
    review_id = str(row.get("id") or "")
    if not review_id:
        return None
    try:
        stars = int(row.get("stars") or 0)
    except (TypeError, ValueError):
        stars = 0
    return WhopReview(
        review_id=review_id,
        user_id=str(row.get("user") or ""),
        product_id=str(row.get("product") or row.get("access_pass") or ""),
        stars=max(0, min(5, stars)),
        title=str(row.get("title") or "").strip(),
        description=str(row.get("description") or "").strip(),
        created_at=int(row.get("created_at") or 0),
    )
