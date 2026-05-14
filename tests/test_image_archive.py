"""Tests for the ImageArchive class.

These don't hit real Telegram or Discord. We mock the bot's
``send_photo`` and the aiohttp session to verify the orchestration:

  - DB cache hit short-circuits the download/upload pair
  - Download failure returns None (caller falls back to URL passthrough)
  - Upload failure returns None
  - Success path writes the file_id back to open_signals.db
  - SSRF guard rejects non-Discord URLs without touching the network
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src.automations.image_archive import ImageArchive
from src.automations.open_signals_db import OpenSignalsDB


@pytest_asyncio.fixture
async def open_signals(tmp_path: Path):
    db = OpenSignalsDB(db_path=str(tmp_path / "open_signals.db"))
    await db.open()
    yield db
    await db.close()


def _photo_size(file_id: str):
    """Mimic Telegram's PhotoSize list element."""
    ps = MagicMock()
    ps.file_id = file_id
    return ps


def _success_message(file_id: str):
    msg = MagicMock()
    msg.photo = [_photo_size(file_id)]
    return msg


def _mock_session(payload: bytes, status: int = 200):
    """Build an aiohttp.ClientSession-like mock whose ``get`` returns an
    async context manager yielding a response with the given payload."""
    session = MagicMock()
    response = MagicMock()
    response.status = status
    response.read = AsyncMock(return_value=payload)
    session.get = MagicMock(return_value=_AsyncCM(response))
    return session


class _AsyncCM:
    """Minimal async context manager wrapper around a value."""
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_cache_hit_short_circuits(open_signals: OpenSignalsDB):
    row_id = await open_signals.record_signal(
        channel_id=1, pair="BTC", side="LONG", leverage=10,
        entry=65000, stop_loss=63000, tp1=67000, tp2=None, tp3=None,
        trade_id=None, raw_message="x",
        image_telegram_file_id="cached_fid_123",
    )
    bot = MagicMock()
    bot.send_photo = AsyncMock()  # should NOT be called
    arch = ImageArchive(
        bot=bot, archive_chat_id=999,
        open_signals_db=open_signals,
        http_session=_mock_session(b""),
    )
    fid = await arch.get_or_upload(
        image_url="https://cdn.discordapp.com/attachments/1/2/x.png", open_signal_id=row_id,
    )
    assert fid == "cached_fid_123"
    bot.send_photo.assert_not_called()


@pytest.mark.asyncio
async def test_download_404_returns_none(open_signals: OpenSignalsDB):
    bot = MagicMock()
    bot.send_photo = AsyncMock()
    arch = ImageArchive(
        bot=bot, archive_chat_id=999,
        open_signals_db=open_signals,
        http_session=_mock_session(b"", status=404),
    )
    fid = await arch.get_or_upload(
        image_url="https://cdn.discordapp.com/attachments/1/2/x.png", open_signal_id=None,
    )
    assert fid is None
    bot.send_photo.assert_not_called()


@pytest.mark.asyncio
async def test_success_path_writes_back_file_id(open_signals: OpenSignalsDB):
    row_id = await open_signals.record_signal(
        channel_id=1, pair="ETH", side="LONG", leverage=5,
        entry=3500, stop_loss=3400, tp1=3700, tp2=None, tp3=None,
        trade_id=None, raw_message="x",
    )
    bot = MagicMock()
    bot.send_photo = AsyncMock(
        return_value=_success_message("uploaded_fid_456"),
    )
    arch = ImageArchive(
        bot=bot, archive_chat_id=999,
        open_signals_db=open_signals,
        http_session=_mock_session(b"\x89PNGfake"),
    )
    fid = await arch.get_or_upload(
        image_url="https://cdn.discordapp.com/attachments/1/2/x.png", open_signal_id=row_id,
    )
    assert fid == "uploaded_fid_456"
    bot.send_photo.assert_awaited_once()
    # Cache write-back: a fresh lookup should now return the same file_id.
    sig = await open_signals.find_by_id(row_id)
    assert sig is not None
    assert sig.image_telegram_file_id == "uploaded_fid_456"


def test_url_allowlist_accepts_discord_cdn():
    from src.automations.image_archive import _is_allowed_image_url
    assert _is_allowed_image_url(
        "https://cdn.discordapp.com/attachments/12345/67890/chart.png"
    )
    assert _is_allowed_image_url("https://media.discordapp.net/attachments/foo")
    assert _is_allowed_image_url("https://images-ext-1.discordapp.net/anything")


def test_url_allowlist_rejects_non_https():
    from src.automations.image_archive import _is_allowed_image_url
    assert not _is_allowed_image_url(
        "http://cdn.discordapp.com/attachments/x/y/z.png"
    )
    assert not _is_allowed_image_url("file:///etc/passwd")
    assert not _is_allowed_image_url("gopher://internal:1234/foo")


def test_url_allowlist_rejects_ip_literals():
    from src.automations.image_archive import _is_allowed_image_url
    assert not _is_allowed_image_url("https://169.254.169.254/latest/meta-data/")
    assert not _is_allowed_image_url("https://127.0.0.1:3001/health")
    assert not _is_allowed_image_url("https://[::1]/x.png")


def test_url_allowlist_rejects_suffix_tricks():
    from src.automations.image_archive import _is_allowed_image_url
    assert not _is_allowed_image_url("https://evil.com/discordapp.com/x")
    assert not _is_allowed_image_url("https://attacker-discordapp.com/x")
    assert not _is_allowed_image_url(
        "https://cdn.discordapp.com.attacker.com/x"
    )


def test_url_allowlist_rejects_internal_services():
    from src.automations.image_archive import _is_allowed_image_url
    assert not _is_allowed_image_url(
        "https://potion-trade-executor.railway.internal:3001/health"
    )
    assert not _is_allowed_image_url("https://localhost/x")


@pytest.mark.asyncio
async def test_ssrf_guard_blocks_internal_url(open_signals: OpenSignalsDB):
    """A bad URL must NEVER reach aiohttp.get OR bot.send_photo."""
    bot = MagicMock()
    bot.send_photo = AsyncMock()
    session = MagicMock()
    session.get = MagicMock(side_effect=AssertionError("network must not be hit"))
    arch = ImageArchive(
        bot=bot, archive_chat_id=999,
        open_signals_db=open_signals,
        http_session=session,
    )
    fid = await arch.get_or_upload(
        image_url="http://169.254.169.254/latest/meta-data/",
        open_signal_id=None,
    )
    assert fid is None
    bot.send_photo.assert_not_called()
    session.get.assert_not_called()


@pytest.mark.asyncio
async def test_empty_url_returns_none(open_signals: OpenSignalsDB):
    bot = MagicMock()
    bot.send_photo = AsyncMock()
    arch = ImageArchive(
        bot=bot, archive_chat_id=999,
        open_signals_db=open_signals,
        http_session=_mock_session(b""),
    )
    fid = await arch.get_or_upload(image_url="", open_signal_id=None)
    assert fid is None
    bot.send_photo.assert_not_called()


@pytest.mark.asyncio
async def test_upload_failure_returns_none(open_signals: OpenSignalsDB):
    from telegram.error import TelegramError

    row_id = await open_signals.record_signal(
        channel_id=1, pair="BTC", side="LONG", leverage=10,
        entry=65000, stop_loss=63000, tp1=67000, tp2=None, tp3=None,
        trade_id=None, raw_message="x",
    )
    bot = MagicMock()
    bot.send_photo = AsyncMock(side_effect=TelegramError("chat not found"))
    arch = ImageArchive(
        bot=bot, archive_chat_id=999,
        open_signals_db=open_signals,
        http_session=_mock_session(b"\x89PNGfake"),
    )
    fid = await arch.get_or_upload(
        image_url="https://cdn.discordapp.com/attachments/1/2/x.png", open_signal_id=row_id,
    )
    assert fid is None
    # Cache should not have been polluted.
    sig = await open_signals.find_by_id(row_id)
    assert sig is not None
    assert sig.image_telegram_file_id is None
