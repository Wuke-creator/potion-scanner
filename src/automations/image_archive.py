"""Permanent image archive for Discord chart screenshots.

Discord CDN URLs are signed and expire after ~24h. To make chart images
permanent (so fan-out works after expiry, and so lifecycle events hours
later can re-attach the original chart), every signal-with-image gets:

  1. Downloaded once from the Discord CDN URL into memory
  2. Uploaded once to a designated Telegram archive chat
  3. The returned ``file_id`` saved into ``open_signals.image_telegram_file_id``

After that, dispatcher fan-out sends use ``send_photo(photo=file_id)``
which is a server-side reference on Telegram's CDN. No re-download, no
re-upload, no Discord involvement. File_ids are valid for the lifetime
of the bot account and reusable across any chat the bot can reach.

Fallback ladder (failures never block alerts):

  - Discord download fails -> return None. Caller falls back to URL
    passthrough on the dispatcher send (~24h CDN signature is fine for
    real-time delivery).
  - Archive upload fails (rate limit, wrong chat id) -> return None.
    Same URL-passthrough fallback.
  - Both fail -> caller sends text-only.

Cache:

  - DB-backed via ``open_signals.image_telegram_file_id``. If the open
    signal row already has a file_id, we return it immediately without
    re-downloading.
  - No in-memory cache layer for v1 — the DB lookup is local SQLite,
    sub-millisecond, and naturally shared across worker tasks.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from io import BytesIO
from urllib.parse import urlparse

import aiohttp
from telegram import Bot
from telegram.error import TelegramError

from src.automations.open_signals_db import OpenSignalsDB

logger = logging.getLogger(__name__)


_DOWNLOAD_TIMEOUT_SEC = 15
_MAX_IMAGE_BYTES = 20 * 1024 * 1024   # Telegram's upload cap is 50MB; 20MB is plenty for charts

# Host allowlist for image downloads. We only fetch from sources we know
# serve images we want to forward. This prevents SSRF: an attacker who
# somehow controls the image_url (admin secret leak, or compromised
# upstream Discord bot) cannot redirect the bot to internal services,
# cloud metadata endpoints (169.254.169.254), or arbitrary intranet hosts.
_ALLOWED_HOST_SUFFIXES: tuple[str, ...] = (
    "discordapp.com",
    "discordapp.net",
    "discord.com",
    "discord.media",
    "cdn.discordapp.com",
)


def _is_allowed_image_url(url: str) -> bool:
    """Return True only when ``url`` is HTTPS and lands on a known image host.

    Rejects:
      - non-HTTPS schemes (file://, http://, gopher://, etc.)
      - IP-literal hosts (so attacker can't bypass the suffix check)
      - hosts that don't match a known Discord CDN suffix
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    # Reject IP literals up front (they can't match a suffix anyway,
    # but this also short-circuits creative "1.2.3.4.nip.io" hostnames
    # that resolve to a public IP).
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _ALLOWED_HOST_SUFFIXES
    )


class ImageArchive:
    """Discord URL -> permanent Telegram file_id, cached in open_signals.db."""

    def __init__(
        self,
        *,
        bot: Bot,
        archive_chat_id: int,
        open_signals_db: OpenSignalsDB,
        http_session: aiohttp.ClientSession | None = None,
    ):
        self._bot = bot
        self._archive_chat_id = archive_chat_id
        self._open_signals_db = open_signals_db
        # Allow caller to share their session; we'll lazy-create one if not.
        self._owns_session = http_session is None
        self._session = http_session

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT_SEC),
            )
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def get_or_upload(
        self,
        *,
        image_url: str,
        open_signal_id: int | None = None,
    ) -> str | None:
        """Resolve a Discord image URL to a permanent Telegram file_id.

        Lookup order:
          1. If ``open_signal_id`` provided and its row has an existing
             ``image_telegram_file_id`` cached -> return it.
          2. Download from Discord URL.
          3. Upload to archive chat -> get file_id.
          4. Write file_id back to the open_signals row (if id supplied).
          5. Return file_id.

        Returns ``None`` if any step fails (caller falls back).
        """
        if not image_url:
            return None

        # SSRF guard: reject anything that isn't a known Discord CDN URL.
        # Required before any DB cache lookup OR network fetch so an
        # attacker who somehow injects a malicious url never reaches our
        # private network. The cache is keyed by signal id (trusted), but
        # we still gate on the URL because a future code path could ever
        # bypass the cache and hit the download directly.
        if not _is_allowed_image_url(image_url):
            logger.warning(
                "image_archive: rejecting non-allowlisted URL %s", image_url,
            )
            return None

        # 1. Cached in DB?
        if open_signal_id is not None:
            try:
                row = await self._open_signals_db.find_by_id(open_signal_id)
                if row is not None and row.image_telegram_file_id:
                    return row.image_telegram_file_id
            except Exception:
                logger.exception(
                    "image_archive: DB lookup failed for signal_id=%d",
                    open_signal_id,
                )

        # 2. Download.
        try:
            session = await self._ensure_session()
            async with session.get(image_url) as resp:
                if resp.status != 200:
                    logger.warning(
                        "image_archive: download HTTP %d for %s",
                        resp.status, image_url,
                    )
                    return None
                payload = await resp.read()
        except Exception:
            logger.exception("image_archive: download failed for %s", image_url)
            return None

        if not payload or len(payload) > _MAX_IMAGE_BYTES:
            logger.warning(
                "image_archive: skipping %s (size=%d bytes)",
                image_url, len(payload) if payload else 0,
            )
            return None

        # 3. Upload to archive chat.
        try:
            message = await self._bot.send_photo(
                chat_id=self._archive_chat_id,
                photo=BytesIO(payload),
                caption=f"signal_id={open_signal_id}" if open_signal_id else None,
                disable_notification=True,
            )
        except TelegramError:
            logger.exception(
                "image_archive: upload to chat=%d failed",
                self._archive_chat_id,
            )
            return None
        except Exception:
            logger.exception("image_archive: unexpected upload error")
            return None

        if not message.photo:
            logger.warning("image_archive: upload returned message with no photo")
            return None

        # Pick the largest size (last element in PhotoSize list per Bot API).
        file_id = message.photo[-1].file_id

        # 4. Persist file_id on the open_signals row.
        if open_signal_id is not None:
            try:
                await self._open_signals_db.update_image_file_id(
                    row_id=open_signal_id,
                    image_telegram_file_id=file_id,
                )
            except Exception:
                logger.exception(
                    "image_archive: failed to persist file_id for signal_id=%d",
                    open_signal_id,
                )
                # Still return the file_id — we got it, the caller can use it
                # for this fan-out even if the cache write failed.

        logger.info(
            "image_archive: uploaded image for signal_id=%s file_id=%s...%s",
            open_signal_id, file_id[:6], file_id[-6:],
        )
        return file_id
