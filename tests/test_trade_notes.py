"""Unit tests for trade notes — CRUD, display, and text handler flow.

Tests:
- Database: update_trade_notes stores and retrieves notes
- Display: _format_trade_detail includes notes when present
- Callback: trade_note_callback sets awaiting state
- Text handler: saves note and shows updated detail
"""

import sqlite3
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.state.database import TradeDatabase
from src.state.models import TradeRecord, TradeStatus
from src.telegram.handlers.trades import (
    _format_trade_detail,
    trade_note_callback,
    trade_note_text_handler,
)


# ------------------------------------------------------------------
# Database tests
# ------------------------------------------------------------------

@pytest.fixture
def trade_db(tmp_path):
    db = TradeDatabase(user_id="user-1", db_path=tmp_path / "test.db")
    return db


def _make_trade(**kwargs):
    defaults = dict(
        trade_id=100, user_id="user-1", pair="BTC/USDT", coin="BTC",
        side="LONG", risk_level="LOW", trade_type="SWING", size_hint="1-4%",
        entry_price=65000.0, stop_loss=63000.0, tp1=67000.0, tp2=69000.0,
        tp3=71000.0, leverage=10, signal_leverage=20,
        position_size_usd=500.0, position_size_coin=0.0077,
        status=TradeStatus.OPEN,
    )
    defaults.update(kwargs)
    return TradeRecord(**defaults)


def test_notes_column_exists(trade_db):
    """Notes column is created in the trades table."""
    row = trade_db._conn.execute(
        "PRAGMA table_info(trades)"
    ).fetchall()
    col_names = [r["name"] for r in row]
    assert "notes" in col_names


def test_update_and_retrieve_notes(trade_db):
    """update_trade_notes stores note and get_trade retrieves it."""
    trade = _make_trade()
    trade_db.create_trade(trade)

    trade_db.update_trade_notes(100, "Great entry point!")
    loaded = trade_db.get_trade(100)
    assert loaded.notes == "Great entry point!"


def test_notes_default_none(trade_db):
    """New trades have notes=None by default."""
    trade = _make_trade()
    trade_db.create_trade(trade)
    loaded = trade_db.get_trade(100)
    assert loaded.notes is None


# ------------------------------------------------------------------
# Display tests
# ------------------------------------------------------------------

def test_format_trade_detail_with_notes():
    """Notes are shown in trade detail when present."""
    trade = _make_trade(notes="Entered on strong support bounce")
    text = _format_trade_detail(trade)
    assert "📝 *Notes:* Entered on strong support bounce" in text


def test_format_trade_detail_without_notes():
    """No notes section when notes is None."""
    trade = _make_trade(notes=None)
    text = _format_trade_detail(trade)
    assert "Notes" not in text


# ------------------------------------------------------------------
# Callback / handler tests
# ------------------------------------------------------------------

def _make_update_and_context(callback_data=None, text=None, user_id="user-1", trade=None):
    """Build mock Update + Context."""
    user_db = MagicMock()
    user_db.get_user_by_telegram_chat_id.return_value = user_id

    trade_db = MagicMock()
    trade_db.get_trade.return_value = trade

    ctx_pipeline = MagicMock()
    ctx_pipeline.db = trade_db

    orchestrator = MagicMock()
    orchestrator.pipelines = {user_id: ctx_pipeline}

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"user_db": user_db, "orchestrator": orchestrator}

    if callback_data:
        query = AsyncMock()
        query.data = callback_data
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        update.effective_chat.id = 12345
        return update, context, trade_db
    else:
        message = AsyncMock()
        message.text = text
        message.reply_text = AsyncMock()

        update = MagicMock()
        update.message = message
        update.effective_chat.id = 12345
        return update, context, trade_db


@pytest.mark.asyncio
async def test_trade_note_callback_sets_awaiting():
    """trade_note_callback sets awaiting_note in user_data."""
    update, context, _ = _make_update_and_context(callback_data="trade_note:100")

    await trade_note_callback(update, context)

    assert context.user_data["awaiting_note"] == 100
    update.callback_query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_trade_note_text_handler_saves_note():
    """Text handler saves note and clears awaiting state."""
    trade = _make_trade(notes="My note")
    update, context, trade_db = _make_update_and_context(text="My note", trade=trade)
    context.user_data["awaiting_note"] = 100

    await trade_note_text_handler(update, context)

    trade_db.update_trade_notes.assert_called_once_with(100, "My note")
    assert "awaiting_note" not in context.user_data
    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_trade_note_text_handler_ignores_without_state():
    """Text handler does nothing if awaiting_note is not set."""
    update, context, trade_db = _make_update_and_context(text="random text")

    await trade_note_text_handler(update, context)

    trade_db.update_trade_notes.assert_not_called()
