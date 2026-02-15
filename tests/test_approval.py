"""Unit tests for trade approval handlers — Approve/Reject/Close Position.

Mocks the orchestrator, exchange client, and database to test:
- Approve on PENDING trade → submit_trade called, message edited
- Reject on PENDING trade → status CANCELED, message edited
- Approve on non-PENDING trade → "no longer pending" message
- Approve with exchange failure → error message, trade canceled
- Close Position → confirmation dialog
- Confirm Close → close_position called, message updated
- Cancel Close → returns to trade detail
- Unregistered user → rejected
- Missing pipeline → error message
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.state.models import TradeRecord, TradeStatus
from src.telegram.handlers.approval import (
    close_trade_callback,
    confirm_close_callback,
    signal_approval_callback,
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_trade(
    trade_id=100,
    status=TradeStatus.PENDING,
    coin="BTC",
    pair="BTC/USDT",
    side="LONG",
    risk_level="LOW",
    trade_type="SWING",
    size_hint="1-4%",
    entry_price=65000.0,
    stop_loss=63000.0,
    tp1=67000.0,
    tp2=69000.0,
    tp3=71000.0,
    leverage=10,
    signal_leverage=20,
    position_size_usd=500.0,
    position_size_coin=0.0077,
):
    return TradeRecord(
        trade_id=trade_id,
        user_id="user-1",
        pair=pair,
        coin=coin,
        side=side,
        risk_level=risk_level,
        trade_type=trade_type,
        size_hint=size_hint,
        entry_price=entry_price,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        leverage=leverage,
        signal_leverage=signal_leverage,
        position_size_usd=position_size_usd,
        position_size_coin=position_size_coin,
        status=status,
        created_at=datetime(2025, 1, 1),
        updated_at=datetime(2025, 1, 1),
    )


def _make_update_and_context(callback_data, chat_id=12345, user_id="user-1", trade=None, pipeline_active=True):
    """Build mock Update + Context for a callback query."""
    query = AsyncMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = chat_id

    # User DB
    user_db = MagicMock()
    user_db.get_user_by_telegram_chat_id.return_value = user_id

    # Trade DB
    trade_db = MagicMock()
    trade_db.get_trade.return_value = trade

    # Pipeline context
    pipeline = MagicMock()
    pipeline._asset_meta = {"BTC": {"szDecimals": 4, "maxLeverage": 50}}

    config = MagicMock()
    config.get_active_preset.return_value.tp_split = [0.33, 0.33, 0.34]
    config.strategy.max_leverage = 20

    client = MagicMock()

    ctx = MagicMock()
    ctx.db = trade_db
    ctx.pipeline = pipeline
    ctx.config = config
    ctx.client = client

    # Orchestrator
    orchestrator = MagicMock()
    if pipeline_active:
        orchestrator.pipelines = {user_id: ctx}
    else:
        orchestrator.pipelines = {}

    context = MagicMock()
    context.bot_data = {
        "user_db": user_db,
        "orchestrator": orchestrator,
    }

    return update, context, ctx


# ------------------------------------------------------------------
# Tests — signal_approval_callback
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_pending_trade_success():
    """Approve on a PENDING trade → submit_trade called, message shows Approved."""
    trade = _make_trade(trade_id=100, status=TradeStatus.PENDING)
    update, context, ctx = _make_update_and_context("signal:approve:100", trade=trade)

    with patch("src.telegram.handlers.approval.build_orders") as mock_build, \
         patch("src.telegram.handlers.approval.PositionManager") as MockPM:
        mock_build.return_value = MagicMock()
        mock_pm = MockPM.return_value
        mock_pm.submit_trade.return_value = True

        await signal_approval_callback(update, context)

        mock_build.assert_called_once()
        mock_pm.submit_trade.assert_called_once()
        query = update.callback_query
        query.edit_message_text.assert_called_once()
        text = query.edit_message_text.call_args.kwargs.get("text", query.edit_message_text.call_args[0][0])
        assert "Approved" in text
        assert "#100" in text


@pytest.mark.asyncio
async def test_approve_pending_trade_exchange_failure():
    """Approve but exchange rejects → error message, trade canceled."""
    trade = _make_trade(trade_id=101, status=TradeStatus.PENDING)
    update, context, ctx = _make_update_and_context("signal:approve:101", trade=trade)

    with patch("src.telegram.handlers.approval.build_orders") as mock_build, \
         patch("src.telegram.handlers.approval.PositionManager") as MockPM:
        mock_build.return_value = MagicMock()
        mock_pm = MockPM.return_value
        mock_pm.submit_trade.return_value = False

        await signal_approval_callback(update, context)

        query = update.callback_query
        text = query.edit_message_text.call_args.kwargs.get("text", query.edit_message_text.call_args[0][0])
        assert "failed" in text.lower()
        ctx.db.update_trade_status.assert_called_with(
            101, TradeStatus.CANCELED, close_reason="submission_failed"
        )


@pytest.mark.asyncio
async def test_approve_pending_trade_exception():
    """Approve but build_orders raises → error message, trade canceled."""
    trade = _make_trade(trade_id=102, status=TradeStatus.PENDING)
    update, context, ctx = _make_update_and_context("signal:approve:102", trade=trade)

    with patch("src.telegram.handlers.approval.build_orders") as mock_build:
        mock_build.side_effect = ValueError("Coin not found")

        await signal_approval_callback(update, context)

        query = update.callback_query
        text = query.edit_message_text.call_args.kwargs.get("text", query.edit_message_text.call_args[0][0])
        assert "failed" in text.lower()
        assert "Coin not found" in text
        ctx.db.update_trade_status.assert_called_with(
            102, TradeStatus.CANCELED, close_reason="approval_error"
        )


@pytest.mark.asyncio
async def test_reject_pending_trade():
    """Reject on a PENDING trade → status CANCELED, message shows Rejected."""
    trade = _make_trade(trade_id=103, status=TradeStatus.PENDING)
    update, context, ctx = _make_update_and_context("signal:reject:103", trade=trade)

    await signal_approval_callback(update, context)

    ctx.db.update_trade_status.assert_called_once_with(
        103, TradeStatus.CANCELED, close_reason="rejected"
    )
    query = update.callback_query
    text = query.edit_message_text.call_args.kwargs.get("text", query.edit_message_text.call_args[0][0])
    assert "Rejected" in text
    assert "#103" in text


@pytest.mark.asyncio
async def test_approve_non_pending_trade():
    """Approve on an OPEN trade → "no longer pending" message."""
    trade = _make_trade(trade_id=104, status=TradeStatus.OPEN)
    update, context, ctx = _make_update_and_context("signal:approve:104", trade=trade)

    await signal_approval_callback(update, context)

    query = update.callback_query
    text = query.edit_message_text.call_args.kwargs.get("text", query.edit_message_text.call_args[0][0])
    assert "no longer pending" in text.lower()


@pytest.mark.asyncio
async def test_approve_trade_not_found():
    """Approve on non-existent trade → "not found" message."""
    update, context, ctx = _make_update_and_context("signal:approve:999", trade=None)

    await signal_approval_callback(update, context)

    query = update.callback_query
    text = query.edit_message_text.call_args.kwargs.get("text", query.edit_message_text.call_args[0][0])
    assert "not found" in text.lower()


@pytest.mark.asyncio
async def test_approve_unregistered_user():
    """Unregistered user clicks Approve → "not registered" message."""
    update, context, _ = _make_update_and_context("signal:approve:100", user_id=None)

    await signal_approval_callback(update, context)

    query = update.callback_query
    text = query.edit_message_text.call_args.kwargs.get("text", query.edit_message_text.call_args[0][0])
    assert "not registered" in text.lower()


@pytest.mark.asyncio
async def test_approve_missing_pipeline():
    """User with no active pipeline clicks Approve → error message."""
    trade = _make_trade(trade_id=105, status=TradeStatus.PENDING)
    update, context, _ = _make_update_and_context(
        "signal:approve:105", trade=trade, pipeline_active=False
    )

    await signal_approval_callback(update, context)

    query = update.callback_query
    text = query.edit_message_text.call_args.kwargs.get("text", query.edit_message_text.call_args[0][0])
    assert "not active" in text.lower()


# ------------------------------------------------------------------
# Tests — close_trade_callback
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_trade_shows_confirmation():
    """Click Close Position → confirmation dialog with Yes/Cancel buttons."""
    update, context, _ = _make_update_and_context("close_trade:200")

    await close_trade_callback(update, context)

    query = update.callback_query
    call_kwargs = query.edit_message_text.call_args.kwargs
    text = call_kwargs.get("text", query.edit_message_text.call_args[0][0])
    assert "Close Trade #200" in text
    assert "market-close" in text.lower()

    markup = call_kwargs["reply_markup"]
    buttons = markup.inline_keyboard[0]
    callback_data = [b.callback_data for b in buttons]
    assert "confirm_close:200" in callback_data
    assert "cancel_close:200" in callback_data


# ------------------------------------------------------------------
# Tests — confirm_close_callback
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_close_success():
    """Confirm close on OPEN trade → close_position called, message updated."""
    trade = _make_trade(trade_id=200, status=TradeStatus.OPEN, coin="ETH")
    update, context, ctx = _make_update_and_context("confirm_close:200", trade=trade)

    with patch("src.telegram.handlers.approval.PositionManager") as MockPM:
        mock_pm = MockPM.return_value
        mock_pm.close_position = MagicMock()

        await confirm_close_callback(update, context)

        mock_pm.close_position.assert_called_once_with(200, "ETH", reason="manual_telegram")
        query = update.callback_query
        text = query.edit_message_text.call_args.kwargs.get("text", query.edit_message_text.call_args[0][0])
        assert "Position Closed" in text
        assert "#200" in text


@pytest.mark.asyncio
async def test_confirm_close_not_open():
    """Confirm close on non-OPEN trade → error message."""
    trade = _make_trade(trade_id=201, status=TradeStatus.CLOSED)
    update, context, ctx = _make_update_and_context("confirm_close:201", trade=trade)

    await confirm_close_callback(update, context)

    query = update.callback_query
    text = query.edit_message_text.call_args.kwargs.get("text", query.edit_message_text.call_args[0][0])
    assert "not open" in text.lower()


@pytest.mark.asyncio
async def test_confirm_close_exception():
    """Confirm close but exchange errors → error message."""
    trade = _make_trade(trade_id=202, status=TradeStatus.OPEN, coin="SOL")
    update, context, ctx = _make_update_and_context("confirm_close:202", trade=trade)

    with patch("src.telegram.handlers.approval.PositionManager") as MockPM:
        mock_pm = MockPM.return_value
        mock_pm.close_position.side_effect = Exception("Connection timeout")

        await confirm_close_callback(update, context)

        query = update.callback_query
        text = query.edit_message_text.call_args.kwargs.get("text", query.edit_message_text.call_args[0][0])
        assert "Close failed" in text
        assert "Connection timeout" in text


@pytest.mark.asyncio
async def test_cancel_close_returns_to_detail():
    """Cancel close → returns to trade detail view with Close Position button."""
    trade = _make_trade(trade_id=203, status=TradeStatus.OPEN, coin="BTC")
    update, context, ctx = _make_update_and_context("cancel_close:203", trade=trade)

    await confirm_close_callback(update, context)

    query = update.callback_query
    call_kwargs = query.edit_message_text.call_args.kwargs
    text = call_kwargs.get("text", query.edit_message_text.call_args[0][0])
    # Should show trade detail (contains trade info)
    assert "#203" in text
    assert "BTC" in text

    # Should have Close Position and Back buttons
    markup = call_kwargs["reply_markup"]
    all_data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "close_trade:203" in all_data
    assert "back:trades" in all_data


@pytest.mark.asyncio
async def test_confirm_close_unregistered_user():
    """Unregistered user confirms close → "not registered" message."""
    update, context, _ = _make_update_and_context("confirm_close:200", user_id=None)

    await confirm_close_callback(update, context)

    query = update.callback_query
    text = query.edit_message_text.call_args.kwargs.get("text", query.edit_message_text.call_args[0][0])
    assert "not registered" in text.lower()


@pytest.mark.asyncio
async def test_confirm_close_missing_pipeline():
    """User with no pipeline confirms close → error message."""
    update, context, _ = _make_update_and_context(
        "confirm_close:200", pipeline_active=False
    )

    await confirm_close_callback(update, context)

    query = update.callback_query
    text = query.edit_message_text.call_args.kwargs.get("text", query.edit_message_text.call_args[0][0])
    assert "not active" in text.lower()


@pytest.mark.asyncio
async def test_confirm_close_trade_not_found():
    """Confirm close on non-existent trade → "not found" message."""
    update, context, _ = _make_update_and_context("confirm_close:999", trade=None)

    await confirm_close_callback(update, context)

    query = update.callback_query
    text = query.edit_message_text.call_args.kwargs.get("text", query.edit_message_text.call_args[0][0])
    assert "not found" in text.lower()
