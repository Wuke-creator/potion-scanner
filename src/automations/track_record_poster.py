"""Public track-record poster for closed Elite calls.

Every terminal close on any forwarded-to-Telegram signal posts a card
to the configured #track-record Discord channel: a title bar (WIN /
LOSS / BREAKEVEN), pair + side + leverage, bullet list of entry / SL /
TP levels, the source channel name, the close timestamp, the chart,
and a footer link. Wins and losses both post. The whole point is
unfiltered.

Idempotency:
    A tiny SQLite table (``track_record_posted``) keyed by ``signal_id``
    blocks double-posts. The same lifecycle event can fire twice on
    rare router retries; we want at most one post per signal.

Chart resolution:
    1. Live closes pass the close-event ``IncomingMessage`` through. If
       the message has an attached Discord image, we download it and
       attach it to the track-record post.
    2. Backfill (no close-event message) falls back to the open-time
       chart cached on ``open_signals.image_telegram_file_id``. We
       download it from Telegram and re-upload to Discord.
    3. If neither path yields bytes, the post is text-only.

Failure handling:
    Every external operation (Discord post, Telegram download, image
    fetch) is wrapped. A failure logs and aborts THAT post, but never
    raises into the caller. The rest of the bot keeps running.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import aiohttp
import aiosqlite
import discord
from telegram import Bot as TelegramBot
from telegram.error import TelegramError

from src.automations.open_signals_db import OpenSignal, OpenSignalsDB

logger = logging.getLogger(__name__)


_RESULT_WIN = "win"
_RESULT_LOSS = "loss"
_RESULT_BREAKEVEN = "breakeven"

_RESULT_LABEL = {
    _RESULT_WIN: "WIN",
    _RESULT_LOSS: "LOSS",
    _RESULT_BREAKEVEN: "BREAKEVEN",
}
_RESULT_COLOR = {
    _RESULT_WIN: 0x2ECC71,        # green
    _RESULT_LOSS: 0xE74C3C,       # red
    _RESULT_BREAKEVEN: 0x95A5A6,  # neutral gray
}

# Statuses that should trigger a track-record post when they land on a
# perps-source signal. ``canceled`` is intentionally excluded: a
# never-filled signal doesn't deserve a record entry.
TERMINAL_STATUSES_POSTABLE: frozenset[str] = frozenset(
    {"closed", "stopped", "all_tp_hit"}
)

_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=15)
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_DISCORD_HOST_SUFFIXES: tuple[str, ...] = (
    "discordapp.com",
    "discordapp.net",
    "discord.com",
    "discord.media",
    "cdn.discordapp.com",
)


def classify_result(
    terminal_status: str,
    prior_status: str | None = None,
) -> str:
    """Pure classifier: terminal_status (+ optional prior_status) -> result.

    ``prior_status`` is whatever ``open_signals.status`` held immediately
    before the terminal flip. When provided, it lets us distinguish a
    stop after partial profit (a win) or a stop after the SL was moved
    to breakeven (a breakeven) from a clean loss. When omitted (typical
    for backfill), we fall back to terminal-status-only classification
    which is conservative: a stop reads as a loss even if the trader
    had moved to BE first.
    """
    if terminal_status == "all_tp_hit":
        return _RESULT_WIN
    if terminal_status == "stopped":
        if prior_status in ("tp_hit", "all_tp_hit"):
            return _RESULT_WIN
        if prior_status == "breakeven":
            return _RESULT_BREAKEVEN
        return _RESULT_LOSS
    if terminal_status == "closed":
        if prior_status in ("tp_hit", "all_tp_hit"):
            return _RESULT_WIN
        return _RESULT_BREAKEVEN
    return _RESULT_BREAKEVEN


def _format_price(value: float | None) -> str:
    """Strip trailing zeros without losing precision on tiny values."""
    if value is None:
        return "-"
    if value == 0:
        return "0"
    # 6 significant digits handles both BTC ($60000) and microcaps (0.0001234)
    s = f"{value:.6g}"
    return s


def _utc_when(unix_ts: int | None) -> datetime:
    if not unix_ts:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(int(unix_ts), tz=timezone.utc)


class TrackRecordPoster:
    """Posts closed perps calls to the public track-record Discord channel."""

    def __init__(
        self,
        *,
        discord_client: discord.Client,
        channel_id: int,
        db_path: str,
        open_signals_db: OpenSignalsDB,
        telegram_bot: TelegramBot | None = None,
        footer_url: str = "",
    ):
        self._client = discord_client
        self._channel_id = channel_id
        self._db_path = db_path
        self._open_signals = open_signals_db
        self._telegram_bot = telegram_bot
        self._footer_url = footer_url.strip()
        self._conn: aiosqlite.Connection | None = None
        self._channel_cache: discord.abc.Messageable | None = None

    async def open(self) -> None:
        if self._conn is not None:
            return
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS track_record_posted (
                signal_id        INTEGER PRIMARY KEY,
                terminal_status  TEXT NOT NULL,
                result           TEXT NOT NULL,
                discord_message_id INTEGER,
                posted_at        INTEGER NOT NULL
            )
            """
        )
        await self._conn.commit()
        logger.info(
            "TrackRecordPoster opened: channel_id=%d db=%s",
            self._channel_id, self._db_path,
        )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError(
                "TrackRecordPoster not opened. Call open() first",
            )
        return self._conn

    async def has_posted(self, signal_id: int) -> bool:
        cur = await self._require_conn().execute(
            "SELECT 1 FROM track_record_posted WHERE signal_id = ?",
            (signal_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return row is not None

    async def _record_posted(
        self,
        *,
        signal_id: int,
        terminal_status: str,
        result: str,
        discord_message_id: int | None,
    ) -> None:
        await self._require_conn().execute(
            "INSERT OR IGNORE INTO track_record_posted "
            "(signal_id, terminal_status, result, discord_message_id, posted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                signal_id, terminal_status, result, discord_message_id,
                int(time.time()),
            ),
        )
        await self._require_conn().commit()

    def render_post(
        self,
        *,
        signal: OpenSignal,
        result: str,
        terminal_status: str,
        caller_name: str | None = None,
        closed_at: datetime | None = None,
    ) -> tuple[str, discord.Embed]:
        """Build the text content + embed for a single closed signal.

        Returns (plaintext_fallback, embed). Discord renders the embed
        when the bot has Embed Links permission; the plaintext_fallback
        is the message body sent alongside it so the post still conveys
        the outcome if embeds get blocked downstream.
        """
        label = _RESULT_LABEL.get(result, "CLOSED")
        color = _RESULT_COLOR.get(result, 0x95A5A6)

        head_bits: list[str] = [signal.pair]
        if signal.side:
            head_bits.append(signal.side.upper())
        if signal.leverage:
            head_bits.append(f"{int(signal.leverage)}x")
        head = " ".join(head_bits)
        title = f"{label}: {head}"

        bullets: list[str] = []
        if signal.entry is not None:
            bullets.append(f"Entry: {_format_price(signal.entry)}")
        if signal.stop_loss is not None:
            sl_line = f"Stop Loss: {_format_price(signal.stop_loss)}"
            if signal.stop_loss_is_conditional:
                sl_line += " (conditional)"
            bullets.append(sl_line)
        for idx, tp in (
            (1, signal.tp1), (2, signal.tp2), (3, signal.tp3),
        ):
            if tp is not None:
                bullets.append(f"TP{idx}: {_format_price(tp)}")

        outcome_line = self._outcome_line(
            terminal_status=terminal_status, result=result,
        )
        if outcome_line:
            bullets.append(f"Outcome: {outcome_line}")
        if caller_name:
            bullets.append(f"Source: {caller_name}")

        when = closed_at or datetime.now(timezone.utc)
        description = "\n".join(bullets)
        if self._footer_url:
            description = (
                f"{description}\n\n[More closed calls]({self._footer_url})"
            )

        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=when,
        )
        if caller_name:
            embed.set_footer(text=f"Called in {caller_name}")
        else:
            embed.set_footer(text="Closed")
        embed.set_image(url="attachment://chart.png")

        # Plaintext fallback (no embed). Used when the channel blocks
        # embeds, or as the message ``content`` next to the embed.
        plain_lines = [title] + bullets
        if self._footer_url:
            plain_lines.append(self._footer_url)
        plaintext = "\n".join(plain_lines)
        return plaintext, embed

    @staticmethod
    def _outcome_line(*, terminal_status: str, result: str) -> str:
        if terminal_status == "all_tp_hit":
            return "All take profits reached"
        if terminal_status == "stopped":
            if result == _RESULT_WIN:
                return "Stopped after partial take profit"
            if result == _RESULT_BREAKEVEN:
                return "Stopped at breakeven"
            return "Stop loss hit"
        if terminal_status == "closed":
            if result == _RESULT_WIN:
                return "Closed in profit"
            return "Closed manually"
        return ""

    # ---- chart resolution -------------------------------------------

    async def _resolve_chart(
        self,
        *,
        signal: OpenSignal,
        close_message_image_urls: list[str] | None,
    ) -> bytes | None:
        """Return chart image bytes (preferring the close-event chart),
        or None if no chart is available."""
        if close_message_image_urls:
            url = close_message_image_urls[0]
            data = await self._download_discord_image(url)
            if data:
                return data
            logger.info(
                "track-record: close-event chart download failed for signal=%s, "
                "falling back to open-time chart",
                signal.id,
            )
        if (
            self._telegram_bot is not None
            and signal.image_telegram_file_id
        ):
            data = await self._download_telegram_file(
                signal.image_telegram_file_id,
            )
            if data:
                return data
        return None

    async def _download_discord_image(self, url: str) -> bytes | None:
        if not _is_allowed_discord_image_url(url):
            logger.warning(
                "track-record: refusing non-Discord image URL: %s", url,
            )
            return None
        try:
            async with aiohttp.ClientSession(
                timeout=_DOWNLOAD_TIMEOUT,
            ) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "track-record: Discord image fetch HTTP %d for %s",
                            resp.status, url,
                        )
                        return None
                    buf = bytearray()
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        buf.extend(chunk)
                        if len(buf) > _MAX_IMAGE_BYTES:
                            logger.warning(
                                "track-record: image %s exceeded %d bytes",
                                url, _MAX_IMAGE_BYTES,
                            )
                            return None
                    return bytes(buf)
        except Exception:
            logger.exception(
                "track-record: Discord image download crashed: %s", url,
            )
            return None

    async def _download_telegram_file(self, file_id: str) -> bytes | None:
        if self._telegram_bot is None:
            return None
        try:
            tg_file = await self._telegram_bot.get_file(file_id)
            bio = BytesIO()
            await tg_file.download_to_memory(bio)
            data = bio.getvalue()
            if len(data) > _MAX_IMAGE_BYTES:
                logger.warning(
                    "track-record: telegram file %s exceeded %d bytes",
                    file_id, _MAX_IMAGE_BYTES,
                )
                return None
            return data
        except TelegramError:
            logger.exception(
                "track-record: telegram file download failed for %s",
                file_id,
            )
            return None
        except Exception:
            logger.exception(
                "track-record: unexpected telegram download error for %s",
                file_id,
            )
            return None

    # ---- posting -----------------------------------------------------

    async def _get_channel(self) -> discord.abc.Messageable | None:
        if self._channel_cache is not None:
            return self._channel_cache
        ch = self._client.get_channel(self._channel_id)
        if ch is None:
            try:
                ch = await self._client.fetch_channel(self._channel_id)
            except discord.NotFound:
                logger.error(
                    "track-record: channel %d not found", self._channel_id,
                )
                return None
            except discord.Forbidden:
                logger.error(
                    "track-record: bot lacks access to channel %d",
                    self._channel_id,
                )
                return None
            except discord.HTTPException:
                logger.exception(
                    "track-record: fetch_channel(%d) failed",
                    self._channel_id,
                )
                return None
        self._channel_cache = ch
        return ch

    async def maybe_post_close(
        self,
        *,
        signal: OpenSignal,
        terminal_status: str,
        prior_status: str | None,
        caller_name: str | None,
        close_message_image_urls: list[str] | None = None,
    ) -> bool:
        """Post a track-record entry for a freshly-closed signal.

        Returns True if a post was actually sent (or already in the
        idempotency table). Returns False on disabled state or
        unrecoverable error. Never raises.
        """
        if self._channel_id == 0:
            return False
        if signal.id is None:
            logger.warning(
                "track-record: skipping signal with no row id (pair=%s)",
                signal.pair,
            )
            return False
        if terminal_status not in TERMINAL_STATUSES_POSTABLE:
            return False

        try:
            if await self.has_posted(signal.id):
                logger.info(
                    "track-record: signal %d already posted, skipping",
                    signal.id,
                )
                return True
        except Exception:
            logger.exception("track-record: idempotency check crashed")
            return False

        result = classify_result(
            terminal_status=terminal_status, prior_status=prior_status,
        )
        return await self._post(
            signal=signal,
            terminal_status=terminal_status,
            result=result,
            caller_name=caller_name,
            close_message_image_urls=close_message_image_urls,
        )

    async def _post(
        self,
        *,
        signal: OpenSignal,
        terminal_status: str,
        result: str,
        caller_name: str | None,
        close_message_image_urls: list[str] | None,
    ) -> bool:
        channel = await self._get_channel()
        if channel is None:
            return False

        chart_bytes = await self._resolve_chart(
            signal=signal,
            close_message_image_urls=close_message_image_urls,
        )
        closed_at = (
            _utc_when(signal.last_event_at)
            if signal.last_event_at else datetime.now(timezone.utc)
        )

        plaintext, embed = self.render_post(
            signal=signal,
            result=result,
            terminal_status=terminal_status,
            caller_name=caller_name,
            closed_at=closed_at,
        )

        file: discord.File | None = None
        if chart_bytes is not None:
            file = discord.File(
                fp=BytesIO(chart_bytes), filename="chart.png",
            )
        else:
            # No chart -> drop the attachment:// reference so the embed
            # doesn't show a broken image placeholder.
            embed.set_image(url=None)

        try:
            kwargs: dict = {"embed": embed}
            if file is not None:
                kwargs["file"] = file
            sent = await channel.send(**kwargs)
            message_id = getattr(sent, "id", None)
        except discord.Forbidden:
            logger.error(
                "track-record: bot forbidden from posting in channel %d",
                self._channel_id,
            )
            return False
        except discord.HTTPException:
            logger.exception(
                "track-record: Discord post failed for signal %d",
                signal.id,
            )
            return False

        try:
            await self._record_posted(
                signal_id=signal.id or 0,
                terminal_status=terminal_status,
                result=result,
                discord_message_id=message_id,
            )
        except Exception:
            logger.exception(
                "track-record: failed to record idempotency row for signal %d",
                signal.id,
            )
        logger.info(
            "track-record: posted signal_id=%s pair=%s result=%s status=%s",
            signal.id, signal.pair, result, terminal_status,
        )
        return True

    # ---- backfill ----------------------------------------------------

    async def backfill(
        self,
        *,
        days: int,
        eligible_channel_ids: Iterable[int],
        channel_display_names: dict[int, str] | None = None,
        pace_sec: float = 2.5,
    ) -> int:
        """Walk closed signals from the last ``days`` and post each.

        ``eligible_channel_ids`` limits which source channels count
        (typically all PERPS channels). ``channel_display_names`` maps
        channel_id -> human-readable name for the caller bullet.

        Idempotency is enforced per-signal so re-running backfill is
        safe. Posts are paced ``pace_sec`` apart to respect Discord
        rate limits (5 posts/5s/channel).

        Returns the number of posts actually sent (excludes already-posted
        signals).
        """
        if self._channel_id == 0:
            return 0
        eligible = set(eligible_channel_ids)
        if not eligible:
            return 0

        cutoff = int(time.time()) - max(1, days) * 86400
        terminal_list = ", ".join(
            f"'{s}'" for s in TERMINAL_STATUSES_POSTABLE
        )
        # Pull from the source open_signals DB directly. We add WHERE
        # clauses on channel_id (perps subset) + opened_at (window) +
        # status (terminal-postable). Ordered chronologically so the
        # channel reads top-down like a real journal.
        placeholders = ", ".join("?" for _ in eligible)
        query = (
            f"SELECT {OpenSignalsDB._SELECT_COLS} "
            "FROM open_signals "
            f"WHERE status IN ({terminal_list}) "
            "  AND opened_at >= ? "
            f"  AND channel_id IN ({placeholders}) "
            "ORDER BY last_event_at ASC, opened_at ASC"
        )
        conn = self._open_signals._require()  # reuse the already-open conn
        cur = await conn.execute(query, (cutoff, *eligible))
        rows = await cur.fetchall()
        await cur.close()

        posted = 0
        names = channel_display_names or {}
        for row in rows:
            signal = OpenSignalsDB._row_to_signal(row)
            if signal.id is None:
                continue
            if await self.has_posted(signal.id):
                continue
            caller = names.get(signal.channel_id)
            sent = await self.maybe_post_close(
                signal=signal,
                terminal_status=signal.status,
                prior_status=None,  # historical: we don't have prior
                caller_name=caller,
                close_message_image_urls=None,
            )
            if sent:
                posted += 1
                await asyncio.sleep(max(0.0, pace_sec))
        logger.info(
            "track-record: backfill complete; %d post(s) in last %d day(s)",
            posted, days,
        )
        return posted


def _is_allowed_discord_image_url(url: str) -> bool:
    """SSRF-safe: only allow HTTPS Discord CDN hosts (no IP literals)."""
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
    # Reject IP literals so an attacker can't bypass suffix check.
    if all(c.isdigit() or c == "." for c in host) or ":" in host:
        return False
    return any(host == s or host.endswith("." + s) for s in _DISCORD_HOST_SUFFIXES)
