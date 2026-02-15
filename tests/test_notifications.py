"""Unit tests for TelegramNotifier — trade lifecycle notifications.

Mocks the telegram Bot to capture sent messages and verifies:
- Each notify_* method produces correct message text
- Missing chat_id is handled gracefully (no crash)
- Exception in send doesn't propagate
- Approve/Reject buttons appear when auto_execute=false
- No buttons when auto_execute=true
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.telegram.notifications import TelegramNotifier


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def mock_bot():
    """A mock telegram.Bot with async send_message."""
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.fixture
def mock_user_db():
    """A mock UserDatabase that returns a chat_id."""
    db = MagicMock()
    db.get_telegram_chat_id.return_value = 12345
    return db


@pytest.fixture
def notifier(mock_bot, mock_user_db):
    """TelegramNotifier wired to mocks."""
    return TelegramNotifier(bot=mock_bot, user_db=mock_user_db, user_id="user-1")


@pytest.fixture
def notifier_no_chat(mock_bot):
    """TelegramNotifier where user has no chat_id."""
    db = MagicMock()
    db.get_telegram_chat_id.return_value = None
    return TelegramNotifier(bot=mock_bot, user_db=db, user_id="user-no-chat")


# ------------------------------------------------------------------
# Helper to extract sent text
# ------------------------------------------------------------------

def _sent_text(mock_bot) -> str:
    """Return the text argument of the last send_message call."""
    mock_bot.send_message.assert_called_once()
    return mock_bot.send_message.call_args.kwargs["text"]


def _sent_markup(mock_bot):
    """Return the reply_markup argument of the last send_message call."""
    mock_bot.send_message.assert_called_once()
    return mock_bot.send_message.call_args.kwargs.get("reply_markup")


# ------------------------------------------------------------------
# Tests — each notify method
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_trade_opened(notifier, mock_bot):
    await notifier.notify_trade_opened(
        trade_id=42, coin="BTC", side="long", entry_price=65000.0, size_usd=500.0,
    )
    text = _sent_text(mock_bot)
    assert "Trade Opened" in text
    assert "#42" in text
    assert "BTC" in text
    assert "LONG" in text
    assert "65000" in text
    assert "$500.00" in text


@pytest.mark.asyncio
async def test_notify_trade_failed(notifier, mock_bot):
    await notifier.notify_trade_failed(trade_id=7, coin="ETH", error="Insufficient margin")
    text = _sent_text(mock_bot)
    assert "Trade Failed" in text
    assert "#7" in text
    assert "ETH" in text
    assert "Insufficient margin" in text


@pytest.mark.asyncio
async def test_notify_tp_hit(notifier, mock_bot):
    await notifier.notify_tp_hit(trade_id=10, coin="SOL", tp_number=2, profit_pct=4.56)
    text = _sent_text(mock_bot)
    assert "TP2 Hit" in text
    assert "#10" in text
    assert "SOL" in text
    assert "+4.56%" in text


@pytest.mark.asyncio
async def test_notify_all_tp_hit(notifier, mock_bot):
    await notifier.notify_all_tp_hit(trade_id=10, coin="SOL", profit_pct=12.34)
    text = _sent_text(mock_bot)
    assert "All TPs Hit" in text
    assert "#10" in text
    assert "+12.34%" in text
    assert "fully closed" in text


@pytest.mark.asyncio
async def test_notify_stop_hit(notifier, mock_bot):
    await notifier.notify_stop_hit(trade_id=5, coin="DOGE", loss_pct=-3.21)
    text = _sent_text(mock_bot)
    assert "Stop Hit" in text
    assert "#5" in text
    assert "DOGE" in text
    assert "-3.21%" in text


@pytest.mark.asyncio
async def test_notify_trade_canceled(notifier, mock_bot):
    await notifier.notify_trade_canceled(trade_id=8, coin="XRP", reason="Signal provider canceled")
    text = _sent_text(mock_bot)
    assert "Trade Canceled" in text
    assert "#8" in text
    assert "Signal provider canceled" in text


@pytest.mark.asyncio
async def test_notify_trade_closed(notifier, mock_bot):
    await notifier.notify_trade_closed(trade_id=9, coin="ADA", detail="Manually closed by provider")
    text = _sent_text(mock_bot)
    assert "Trade Closed" in text
    assert "#9" in text
    assert "Manually closed by provider" in text


@pytest.mark.asyncio
async def test_notify_breakeven(notifier, mock_bot):
    await notifier.notify_breakeven(trade_id=3, coin="ETH", entry_price=3200.50)
    text = _sent_text(mock_bot)
    assert "Breakeven" in text
    assert "#3" in text
    assert "3200.5" in text


@pytest.mark.asyncio
async def test_notify_sl_moved(notifier, mock_bot):
    await notifier.notify_sl_moved(trade_id=4, coin="BTC", new_price=64500.0)
    text = _sent_text(mock_bot)
    assert "SL Moved" in text
    assert "#4" in text
    assert "64500" in text


@pytest.mark.asyncio
async def test_notify_risk_warning(notifier, mock_bot):
    await notifier.notify_risk_warning("Approaching max exposure limit (90%)")
    text = _sent_text(mock_bot)
    assert "Risk Warning" in text
    assert "Approaching max exposure" in text


# ------------------------------------------------------------------
# Tests — new signal with/without auto_execute
# ------------------------------------------------------------------

def _make_signal(trade_id=1):
    """Create a minimal mock signal object."""
    sig = MagicMock()
    sig.trade_id = trade_id
    sig.pair = "BTCUSDT"
    sig.side.value = "long"
    sig.entry = 65000.0
    sig.stop_loss = 63000.0
    return sig


def _make_trade_set(leverage=10):
    """Create a minimal mock trade set."""
    ts = MagicMock()
    ts.leverage = leverage
    return ts


@pytest.mark.asyncio
async def test_notify_new_signal_auto_execute_true(notifier, mock_bot):
    """When auto_execute=true, no inline buttons."""
    signal = _make_signal(trade_id=99)
    trade_set = _make_trade_set(leverage=5)

    await notifier.notify_new_signal(signal, trade_set, position_size_usd=250.0, auto_execute=True)

    text = _sent_text(mock_bot)
    assert "New Signal" in text
    assert "#99" in text
    assert "BTCUSDT" in text
    assert "LONG" in text
    assert "$250.00" in text
    assert "5x" in text

    markup = _sent_markup(mock_bot)
    assert markup is None


@pytest.mark.asyncio
async def test_notify_new_signal_auto_execute_false(notifier, mock_bot):
    """When auto_execute=false, Approve/Reject buttons should appear."""
    signal = _make_signal(trade_id=77)
    trade_set = _make_trade_set(leverage=20)

    await notifier.notify_new_signal(signal, trade_set, position_size_usd=100.0, auto_execute=False)

    text = _sent_text(mock_bot)
    assert "New Signal" in text
    assert "#77" in text
    assert "Auto-execute is OFF" in text

    markup = _sent_markup(mock_bot)
    assert markup is not None

    # Extract button labels from the inline keyboard
    buttons = markup.inline_keyboard[0]
    labels = [b.text for b in buttons]
    assert "Approve" in labels
    assert "Reject" in labels

    # Check callback data
    callback_data = [b.callback_data for b in buttons]
    assert "signal:approve:77" in callback_data
    assert "signal:reject:77" in callback_data


# ------------------------------------------------------------------
# Tests — graceful handling
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_crash_when_no_chat_id(notifier_no_chat, mock_bot):
    """When user has no chat_id, methods should silently skip."""
    await notifier_no_chat.notify_trade_opened(
        trade_id=1, coin="BTC", side="long", entry_price=65000, size_usd=100,
    )
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_no_crash_when_send_fails(notifier, mock_bot):
    """When send_message raises, the exception should not propagate."""
    mock_bot.send_message.side_effect = Exception("Network error")

    # Should not raise
    await notifier.notify_trade_opened(
        trade_id=1, coin="BTC", side="long", entry_price=65000, size_usd=100,
    )


@pytest.mark.asyncio
async def test_chat_id_refresh_on_none(mock_bot):
    """If chat_id is initially None, it should re-check the database."""
    db = MagicMock()
    # First call returns None (during __init__), second call returns a value
    db.get_telegram_chat_id.side_effect = [None, 99999]

    notifier = TelegramNotifier(bot=mock_bot, user_db=db, user_id="user-late")
    await notifier.notify_risk_warning("test")

    # Should have been called twice — once in __init__, once in _get_chat_id
    assert db.get_telegram_chat_id.call_count == 2
    mock_bot.send_message.assert_called_once()
    assert mock_bot.send_message.call_args.kwargs["chat_id"] == 99999
