"""Tests for the public track-record poster.

We don't hit real Discord or Telegram. The Discord client and the
Telegram bot are mocked; aiohttp downloads are bypassed by passing
pre-rendered close-event image URLs that the resolver short-circuits
on the SSRF check (we don't actually attempt a fetch in the unit path).

Covered:
  - classify_result: pure function across win / loss / breakeven cases
  - render_post: win, loss, breakeven, with and without caller_name
  - has_posted / _record_posted: idempotency table semantics
  - maybe_post_close: end-to-end with mocked Discord channel, including
    idempotency, gating on terminal status, and channel-not-reachable
  - backfill: walks the open_signals DB, paces posts, respects
    idempotency, only posts eligible perps channels
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src.automations.open_signals_db import OpenSignal, OpenSignalsDB
from src.automations.track_record_poster import (
    TERMINAL_STATUSES_POSTABLE,
    TrackRecordPoster,
    classify_result,
)


# ------------------------------ helpers ------------------------------


def _make_signal(
    *,
    signal_id: int = 1,
    channel_id: int = 100,
    pair: str = "WET/USDT",
    side: str | None = "SHORT",
    leverage: int | None = 50,
    entry: float | None = 0.099,
    stop_loss: float | None = 0.105,
    tp1: float | None = 0.094,
    tp2: float | None = 0.090,
    tp3: float | None = 0.085,
    status: str = "all_tp_hit",
    opened_at: int | None = None,
    last_event_at: int | None = None,
    image_telegram_file_id: str | None = None,
) -> OpenSignal:
    now = int(time.time())
    return OpenSignal(
        channel_id=channel_id,
        pair=pair,
        normalised_base=pair.split("/")[0],
        side=side,
        leverage=leverage,
        entry=entry,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        trade_id=None,
        status=status,
        opened_at=opened_at or now - 3600,
        last_event_at=last_event_at or now,
        raw_message="raw",
        stop_loss_is_conditional=False,
        image_telegram_file_id=image_telegram_file_id,
        id=signal_id,
    )


def _mock_discord_channel_with_send(message_id: int = 999_888):
    """Build a fake discord channel whose .send(...) returns a Message-like."""
    sent_msg = MagicMock()
    sent_msg.id = message_id
    channel = MagicMock()
    channel.send = AsyncMock(return_value=sent_msg)
    return channel


def _mock_client(channel) -> MagicMock:
    client = MagicMock()
    client.get_channel = MagicMock(return_value=channel)
    client.fetch_channel = AsyncMock(return_value=channel)
    return client


@pytest_asyncio.fixture
async def open_signals(tmp_path: Path):
    db = OpenSignalsDB(db_path=str(tmp_path / "open_signals.db"))
    await db.open()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def poster(tmp_path: Path, open_signals: OpenSignalsDB):
    channel = _mock_discord_channel_with_send()
    client = _mock_client(channel)
    poster = TrackRecordPoster(
        discord_client=client,
        channel_id=12345,
        db_path=str(tmp_path / "track_record.db"),
        open_signals_db=open_signals,
        telegram_bot=None,
        footer_url="https://discord.com/channels/1/12345",
    )
    await poster.open()
    # Stash for tests to inspect calls.
    poster._test_channel = channel  # type: ignore[attr-defined]
    poster._test_client = client    # type: ignore[attr-defined]
    yield poster
    await poster.close()


# ------------------------------ classify_result ------------------------------


def test_classify_result_all_tp_hit_is_always_win():
    assert classify_result("all_tp_hit") == "win"
    assert classify_result("all_tp_hit", prior_status="open") == "win"
    assert classify_result("all_tp_hit", prior_status="tp_hit") == "win"


def test_classify_result_stopped_without_prior_context_is_loss():
    assert classify_result("stopped") == "loss"
    assert classify_result("stopped", prior_status="open") == "loss"


def test_classify_result_stopped_after_breakeven_is_breakeven():
    assert classify_result("stopped", prior_status="breakeven") == "breakeven"


def test_classify_result_stopped_after_partial_tp_is_win():
    assert classify_result("stopped", prior_status="tp_hit") == "win"
    assert classify_result("stopped", prior_status="all_tp_hit") == "win"


def test_classify_result_closed_default_is_breakeven():
    assert classify_result("closed") == "breakeven"
    assert classify_result("closed", prior_status="open") == "breakeven"


def test_classify_result_closed_after_tp_is_win():
    assert classify_result("closed", prior_status="tp_hit") == "win"


def test_classify_result_unknown_status_defaults_breakeven():
    assert classify_result("garbage") == "breakeven"


# ------------------------------ render_post ------------------------------


def test_render_post_win_with_caller(poster: TrackRecordPoster):
    signal = _make_signal(status="all_tp_hit")
    plaintext, embed = poster.render_post(
        signal=signal,
        result="win",
        terminal_status="all_tp_hit",
        caller_name="#perp-calls-pingu",
    )
    assert plaintext.startswith("WIN: WET/USDT SHORT 50x")
    assert "Entry: 0.099" in plaintext
    assert "Stop Loss: 0.105" in plaintext
    assert "TP1: 0.094" in plaintext
    assert "TP2: 0.09" in plaintext
    assert "TP3: 0.085" in plaintext
    assert "All take profits reached" in plaintext
    assert "#perp-calls-pingu" in plaintext
    assert embed.title is not None and embed.title.startswith("WIN")
    assert embed.color is not None and embed.color.value == 0x2ECC71


def test_render_post_loss_renders_loss_label(poster: TrackRecordPoster):
    signal = _make_signal(status="stopped")
    plaintext, embed = poster.render_post(
        signal=signal,
        result="loss",
        terminal_status="stopped",
        caller_name="#perp-calls-pingu",
    )
    assert plaintext.startswith("LOSS: WET/USDT SHORT 50x")
    assert "Stop loss hit" in plaintext
    assert embed.color is not None and embed.color.value == 0xE74C3C


def test_render_post_breakeven_label(poster: TrackRecordPoster):
    signal = _make_signal(status="stopped")
    plaintext, embed = poster.render_post(
        signal=signal,
        result="breakeven",
        terminal_status="stopped",
        caller_name="#perp-calls-pingu",
    )
    assert plaintext.startswith("BREAKEVEN: WET/USDT SHORT 50x")
    assert "Stopped at breakeven" in plaintext
    assert embed.color is not None and embed.color.value == 0x95A5A6


def test_render_post_without_caller(poster: TrackRecordPoster):
    signal = _make_signal(status="all_tp_hit")
    plaintext, embed = poster.render_post(
        signal=signal,
        result="win",
        terminal_status="all_tp_hit",
        caller_name=None,
    )
    assert "Source:" not in plaintext
    # Embed footer falls back to a neutral "Closed"
    assert embed.footer is not None
    assert embed.footer.text == "Closed"


def test_render_post_omits_missing_levels(poster: TrackRecordPoster):
    signal = _make_signal(
        status="all_tp_hit", tp2=None, tp3=None, stop_loss=None,
    )
    plaintext, _ = poster.render_post(
        signal=signal,
        result="win",
        terminal_status="all_tp_hit",
        caller_name=None,
    )
    assert "TP2" not in plaintext
    assert "TP3" not in plaintext
    assert "Stop Loss" not in plaintext


def test_render_post_includes_footer_url_when_configured(
    poster: TrackRecordPoster,
):
    signal = _make_signal()
    plaintext, embed = poster.render_post(
        signal=signal,
        result="win",
        terminal_status="all_tp_hit",
        caller_name=None,
    )
    assert "https://discord.com/channels/1/12345" in plaintext
    assert embed.description is not None
    assert "https://discord.com/channels/1/12345" in embed.description


# ------------------------------ idempotency ------------------------------


@pytest.mark.asyncio
async def test_has_posted_false_then_true_after_record(
    poster: TrackRecordPoster,
):
    assert await poster.has_posted(42) is False
    await poster._record_posted(
        signal_id=42, terminal_status="all_tp_hit", result="win",
        discord_message_id=123,
    )
    assert await poster.has_posted(42) is True


@pytest.mark.asyncio
async def test_record_posted_is_idempotent_on_signal_id(
    poster: TrackRecordPoster,
):
    await poster._record_posted(
        signal_id=99, terminal_status="stopped", result="loss",
        discord_message_id=1,
    )
    # Second insert with same signal_id must NOT raise (INSERT OR IGNORE).
    await poster._record_posted(
        signal_id=99, terminal_status="stopped", result="loss",
        discord_message_id=2,
    )
    cur = await poster._require_conn().execute(
        "SELECT COUNT(*) FROM track_record_posted WHERE signal_id = ?",
        (99,),
    )
    row = await cur.fetchone()
    await cur.close()
    assert row is not None and row[0] == 1


# ------------------------------ maybe_post_close ------------------------------


@pytest.mark.asyncio
async def test_maybe_post_close_posts_and_records_idempotency(
    poster: TrackRecordPoster,
):
    signal = _make_signal(signal_id=7, status="all_tp_hit")
    ok = await poster.maybe_post_close(
        signal=signal,
        terminal_status="all_tp_hit",
        prior_status="tp_hit",
        caller_name="#perp-calls-pingu",
        close_message_image_urls=None,
    )
    assert ok is True
    poster._test_channel.send.assert_awaited_once()  # type: ignore[attr-defined]
    assert await poster.has_posted(7) is True


@pytest.mark.asyncio
async def test_maybe_post_close_is_idempotent(
    poster: TrackRecordPoster,
):
    signal = _make_signal(signal_id=8, status="all_tp_hit")
    first = await poster.maybe_post_close(
        signal=signal,
        terminal_status="all_tp_hit",
        prior_status=None,
        caller_name=None,
        close_message_image_urls=None,
    )
    second = await poster.maybe_post_close(
        signal=signal,
        terminal_status="all_tp_hit",
        prior_status=None,
        caller_name=None,
        close_message_image_urls=None,
    )
    assert first is True
    assert second is True  # treated as already-posted, not an error
    # send should have been called exactly once across the two attempts.
    poster._test_channel.send.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_maybe_post_close_skips_non_postable_status(
    poster: TrackRecordPoster,
):
    signal = _make_signal(signal_id=9, status="canceled")
    ok = await poster.maybe_post_close(
        signal=signal,
        terminal_status="canceled",
        prior_status="open",
        caller_name=None,
        close_message_image_urls=None,
    )
    assert ok is False
    poster._test_channel.send.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_maybe_post_close_returns_false_when_channel_unreachable(
    tmp_path: Path, open_signals: OpenSignalsDB,
):
    client = MagicMock()
    client.get_channel = MagicMock(return_value=None)

    async def _fetch_404(_id):
        import discord
        raise discord.NotFound(MagicMock(status=404), "not found")

    client.fetch_channel = AsyncMock(side_effect=_fetch_404)
    p = TrackRecordPoster(
        discord_client=client,
        channel_id=42,
        db_path=str(tmp_path / "tr.db"),
        open_signals_db=open_signals,
        footer_url="",
    )
    await p.open()
    try:
        signal = _make_signal(signal_id=10)
        ok = await p.maybe_post_close(
            signal=signal,
            terminal_status="all_tp_hit",
            prior_status=None,
            caller_name=None,
            close_message_image_urls=None,
        )
        assert ok is False
        assert await p.has_posted(10) is False
    finally:
        await p.close()


@pytest.mark.asyncio
async def test_maybe_post_close_disabled_when_channel_id_zero(
    tmp_path: Path, open_signals: OpenSignalsDB,
):
    client = MagicMock()
    p = TrackRecordPoster(
        discord_client=client,
        channel_id=0,
        db_path=str(tmp_path / "tr.db"),
        open_signals_db=open_signals,
    )
    await p.open()
    try:
        signal = _make_signal(signal_id=11)
        ok = await p.maybe_post_close(
            signal=signal,
            terminal_status="all_tp_hit",
            prior_status=None,
            caller_name=None,
            close_message_image_urls=None,
        )
        assert ok is False
        client.get_channel.assert_not_called()
    finally:
        await p.close()


# ------------------------------ backfill ------------------------------


@pytest.mark.asyncio
async def test_backfill_filters_by_eligible_channels_and_respects_idempotency(
    poster: TrackRecordPoster, open_signals: OpenSignalsDB,
):
    # Seed four signals: two in eligible channels in window, one in a
    # non-eligible channel that must be ignored, and one terminal-but-
    # not-postable (canceled) that must also be ignored.
    rid_a = await open_signals.record_signal(
        channel_id=100, pair="ETH/USDT", side="LONG", leverage=10,
        entry=3000, stop_loss=2900, tp1=3100, tp2=3200, tp3=3300,
        trade_id=None, raw_message="a",
    )
    rid_b = await open_signals.record_signal(
        channel_id=200, pair="BTC/USDT", side="SHORT", leverage=20,
        entry=60000, stop_loss=61000, tp1=59000, tp2=58000, tp3=57000,
        trade_id=None, raw_message="b",
    )
    rid_c = await open_signals.record_signal(
        channel_id=999, pair="MEMECOIN", side="LONG", leverage=1,
        entry=0.1, stop_loss=0.09, tp1=0.11, tp2=None, tp3=None,
        trade_id=None, raw_message="c",
    )
    rid_d = await open_signals.record_signal(
        channel_id=100, pair="SOL/USDT", side="LONG", leverage=5,
        entry=150, stop_loss=140, tp1=160, tp2=170, tp3=180,
        trade_id=None, raw_message="d",
    )
    # Flip statuses
    await open_signals.update_status(
        channel_id=100, pair_or_base="ETH", new_status="all_tp_hit",
    )
    await open_signals.update_status(
        channel_id=200, pair_or_base="BTC", new_status="stopped",
    )
    await open_signals.update_status(
        channel_id=999, pair_or_base="MEMECOIN", new_status="all_tp_hit",
    )
    await open_signals.update_status(
        channel_id=100, pair_or_base="SOL", new_status="canceled",
    )

    # Eligible = the two perps channels; the third (999) is excluded.
    eligible = {100, 200}
    names = {100: "#perp-calls-a", 200: "#perp-calls-b"}

    count = await poster.backfill(
        days=30,
        eligible_channel_ids=eligible,
        channel_display_names=names,
        pace_sec=0.0,
    )
    assert count == 2  # ETH + BTC, not MEMECOIN (not eligible) or SOL (canceled)
    assert await poster.has_posted(rid_a) is True
    assert await poster.has_posted(rid_b) is True
    assert await poster.has_posted(rid_c) is False
    assert await poster.has_posted(rid_d) is False

    # Re-running backfill is a no-op: idempotency table blocks dupes.
    count2 = await poster.backfill(
        days=30,
        eligible_channel_ids=eligible,
        channel_display_names=names,
        pace_sec=0.0,
    )
    assert count2 == 0


@pytest.mark.asyncio
async def test_backfill_skips_when_no_eligible_channels(
    poster: TrackRecordPoster, open_signals: OpenSignalsDB,
):
    await open_signals.record_signal(
        channel_id=100, pair="ETH/USDT", side="LONG", leverage=10,
        entry=3000, stop_loss=2900, tp1=3100, tp2=3200, tp3=3300,
        trade_id=None, raw_message="a",
    )
    await open_signals.update_status(
        channel_id=100, pair_or_base="ETH", new_status="all_tp_hit",
    )
    count = await poster.backfill(
        days=30, eligible_channel_ids=set(), pace_sec=0.0,
    )
    assert count == 0


@pytest.mark.asyncio
async def test_backfill_disabled_when_channel_id_zero(
    tmp_path: Path, open_signals: OpenSignalsDB,
):
    client = MagicMock()
    p = TrackRecordPoster(
        discord_client=client,
        channel_id=0,
        db_path=str(tmp_path / "tr.db"),
        open_signals_db=open_signals,
    )
    await p.open()
    try:
        count = await p.backfill(
            days=30, eligible_channel_ids={1, 2}, pace_sec=0.0,
        )
        assert count == 0
    finally:
        await p.close()


# ------------------------------ constants ------------------------------


def test_terminal_statuses_postable_excludes_canceled():
    assert "all_tp_hit" in TERMINAL_STATUSES_POSTABLE
    assert "stopped" in TERMINAL_STATUSES_POSTABLE
    assert "closed" in TERMINAL_STATUSES_POSTABLE
    assert "canceled" not in TERMINAL_STATUSES_POSTABLE
