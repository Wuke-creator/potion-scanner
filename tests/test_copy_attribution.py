"""Phase 4 tests: copy_trades attribution table, the engine's confirm-time
price-deviation gate, and the watcher's skip shadow-logging + reconciler.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.config.settings import AutotradeConfig, WalletCopyConfig
from src.trading.autotrade_engine import AutotradeEngine
from src.trading.autotrade_prefs_db import AutotradePrefsDB
from src.trading.hyperliquid_client import AccountSnapshot
from src.trading.venue import VenueConnection, VenuePlan, VenueResult
from src.trading.wallet_metrics_db import TrackedWallet, WalletMetricsDB

UID = 111
LEADER = "0xadd12adbbd5db87674b38af99b6dd34dd2a45e0d"

_PLAN = VenuePlan(coin="HYPE", is_long=True, price=25.0, size=40.0,
                  leverage=10, notional_usd=1000.0)
_RESULT = VenueResult(coin="HYPE", size=40.0, ref="ord-77",
                      tp_legs=[(28.0, 20.0)])


@pytest_asyncio.fixture
async def dbs(tmp_path: Path):
    prefs = AutotradePrefsDB(db_path=str(tmp_path / "prefs.db"))
    await prefs.open()
    metrics = WalletMetricsDB(db_path=str(tmp_path / "scout.db"))
    await metrics.open()
    yield prefs, metrics
    await metrics.close()
    await prefs.close()


def _wallet_sig(**kw):
    base = dict(
        pair="HYPE/USDT", side="LONG", leverage=10, entry=25.0,
        take_profits=[28.0, 31.0, 34.0], stop_loss=22.0,
        size_pct_override=3.0, note="ATR-derived levels.",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _meta(**kw):
    base = dict(
        leader_address=LEADER, coin="HYPE", inst_id="HYPE-USDT",
        proposal_price=25.0, atr=2.0, max_deviation_atr=0.6,
    )
    base.update(kw)
    return base


def _engine(prefs, metrics, *, current_price=25.0, price_error=False):
    cfg = AutotradeConfig(
        enabled=True, dry_run=False, venue="blofin", network="mainnet",
        allowlist=frozenset({UID}), default_size_pct=5.0, max_leverage=100,
        max_per_day=10, min_collateral_usdc=5.0, slippage_bps=100,
        risk_enabled=False,
    )
    venue = AsyncMock()
    venue.name = "blofin"
    venue.supports_risk_guard = False
    venue.get_connection = AsyncMock(
        return_value=VenueConnection(is_active=True, label="k"),
    )
    venue.get_balance = AsyncMock(return_value=1000.0)
    venue.get_account_snapshot = AsyncMock(
        return_value=AccountSnapshot(account_value=1000.0, positions={}),
    )
    venue.plan = AsyncMock(return_value=_PLAN)
    venue.place = AsyncMock(return_value=_RESULT)
    venue.mark_success = AsyncMock()
    venue.mark_failure = AsyncMock()
    if price_error:
        venue.get_price = AsyncMock(side_effect=RuntimeError("api down"))
    else:
        venue.get_price = AsyncMock(return_value=current_price)
    verification = AsyncMock()
    verification.get_verified = AsyncMock(
        return_value=SimpleNamespace(is_active=True),
    )
    send_dm = AsyncMock()
    engine = AutotradeEngine(
        config=cfg, max_collateral_usdc=5000.0, venue=venue,
        prefs_db=prefs, verification_db=verification, send_dm=send_dm,
        copy_store=metrics,
    )
    return engine, venue, send_dm


async def _opt_in(prefs):
    await prefs.set_enabled(UID, True)
    await prefs.accept_disclosure(UID)


class TestCopyTradesTable:
    @pytest.mark.asyncio
    async def test_lifecycle_roundtrip(self, dbs):
        _, metrics = dbs
        tid = await metrics.insert_copy_trade(
            leader_address=LEADER, coin="HYPE", inst_id="HYPE-USDT",
            telegram_user_id=UID, side="LONG", proposal_id=555,
            proposal_price=25.0, atr_at_proposal=2.0,
        )
        row = await metrics.get_copy_trade(555, UID)
        assert row is not None and row.status == "proposed"
        await metrics.mark_copy_trade_filled(
            555, UID, order_ref="ord-1", size_base=40.0, leverage=10,
        )
        open_rows = await metrics.open_copy_trades(LEADER, "HYPE")
        assert len(open_rows) == 1 and open_rows[0].order_ref == "ord-1"
        await metrics.close_copy_trade(
            tid, close_reason="leader_exit_mirror", realized_pnl=12.5,
        )
        assert await metrics.open_copy_trades(LEADER) == []
        assert await metrics.leader_realized_pnl(LEADER) == pytest.approx(12.5)

    @pytest.mark.asyncio
    async def test_unreconciled_excluded_from_leader_pnl(self, dbs):
        _, metrics = dbs
        tid = await metrics.insert_copy_trade(
            leader_address=LEADER, coin="ZEC", inst_id="ZEC-USDT",
            telegram_user_id=UID, side="SHORT", proposal_id=556,
            proposal_price=None, atr_at_proposal=None,
        )
        await metrics.mark_copy_trade_filled(
            556, UID, order_ref="ord-2", size_base=1.0, leverage=5,
        )
        await metrics.close_copy_trade(
            tid, close_reason="manual", realized_pnl=None,
            status="closed_unreconciled",
        )
        assert await metrics.leader_realized_pnl(LEADER) == 0.0

    @pytest.mark.asyncio
    async def test_status_update_only_touches_proposed(self, dbs):
        _, metrics = dbs
        await metrics.insert_copy_trade(
            leader_address=LEADER, coin="HYPE", inst_id="HYPE-USDT",
            telegram_user_id=UID, side="LONG", proposal_id=557,
            proposal_price=25.0, atr_at_proposal=2.0,
        )
        await metrics.mark_copy_trade_filled(
            557, UID, order_ref="ord-3", size_base=1.0, leverage=1,
        )
        await metrics.set_copy_trade_status(557, UID, "expired")
        row = await metrics.get_copy_trade(557, UID)
        assert row.status == "filled"   # filled rows never regress


class TestDeviationGate:
    @pytest.mark.asyncio
    async def test_within_tolerance_fires_and_attributes(self, dbs):
        prefs, metrics = dbs
        engine, venue, _ = _engine(prefs, metrics, current_price=25.5)
        await _opt_in(prefs)
        pid = await engine.propose_copy(
            _wallet_sig(), source="wallet x", meta=_meta(),
        )
        assert pid is not None
        row = await metrics.get_copy_trade(pid, UID)
        assert row is not None and row.status == "proposed"
        ok = await engine.confirm_copy(UID)
        assert ok is True
        venue.place.assert_awaited_once()
        row = await metrics.get_copy_trade(pid, UID)
        assert row.status == "filled" and row.order_ref == "ord-77"

    @pytest.mark.asyncio
    async def test_adverse_move_cancels(self, dbs):
        prefs, metrics = dbs
        # long proposed at 25.0, ATR 2.0, limit 0.6 ATR = 1.2 -> 26.5 is out
        engine, venue, send_dm = _engine(prefs, metrics, current_price=26.5)
        await _opt_in(prefs)
        pid = await engine.propose_copy(
            _wallet_sig(), source="wallet x", meta=_meta(),
        )
        ok = await engine.confirm_copy(UID)
        assert ok is False
        venue.place.assert_not_called()
        row = await metrics.get_copy_trade(pid, UID)
        assert row.status == "cancelled_deviation"
        assert "Auto-cancelled" in send_dm.await_args.args[1]
        # pending is gone: a second confirm finds nothing
        ok2 = await engine.confirm_copy(UID)
        assert ok2 is False
        assert "No pending" in send_dm.await_args.args[1]

    @pytest.mark.asyncio
    async def test_favorable_move_passes(self, dbs):
        prefs, metrics = dbs
        # price DROPPED for a long: better entry, never cancel
        engine, venue, _ = _engine(prefs, metrics, current_price=23.0)
        await _opt_in(prefs)
        await engine.propose_copy(_wallet_sig(), source="w", meta=_meta())
        assert await engine.confirm_copy(UID) is True
        venue.place.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_short_side_gate_mirrored(self, dbs):
        prefs, metrics = dbs
        # short proposed at 25.0; price dumping to 23.0 = adverse for entry
        engine, venue, _ = _engine(prefs, metrics, current_price=23.0)
        await _opt_in(prefs)
        pid = await engine.propose_copy(
            _wallet_sig(side="SHORT", stop_loss=28.0,
                        take_profits=[22.0, 19.0, 16.0]),
            source="w", meta=_meta(),
        )
        assert await engine.confirm_copy(UID) is False
        row = await metrics.get_copy_trade(pid, UID)
        assert row.status == "cancelled_deviation"

    @pytest.mark.asyncio
    async def test_unreadable_price_keeps_pending(self, dbs):
        prefs, metrics = dbs
        engine, venue, send_dm = _engine(prefs, metrics, price_error=True)
        await _opt_in(prefs)
        pid = await engine.propose_copy(_wallet_sig(), source="w", meta=_meta())
        ok = await engine.confirm_copy(UID)
        assert ok is False
        venue.place.assert_not_called()
        assert "still pending" in send_dm.await_args.args[1]
        # price comes back: the SAME pending proposal can now fire
        venue.get_price = AsyncMock(return_value=25.1)
        assert await engine.confirm_copy(UID) is True
        row = await metrics.get_copy_trade(pid, UID)
        assert row.status == "filled"

    @pytest.mark.asyncio
    async def test_no_meta_skips_gate_entirely(self, dbs):
        prefs, metrics = dbs
        engine, venue, _ = _engine(prefs, metrics, current_price=99999.0)
        await _opt_in(prefs)
        await engine.propose_copy(_wallet_sig(), source="cabal")   # no meta
        assert await engine.confirm_copy(UID) is True
        venue.get_price.assert_not_called()
        venue.place.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_atr_in_meta_passes_gate(self, dbs):
        prefs, metrics = dbs
        engine, venue, _ = _engine(prefs, metrics, current_price=99999.0)
        await _opt_in(prefs)
        await engine.propose_copy(
            _wallet_sig(), source="w", meta=_meta(atr=None),
        )
        assert await engine.confirm_copy(UID) is True
        venue.get_price.assert_not_called()

    @pytest.mark.asyncio
    async def test_expiry_marks_row(self, dbs):
        import time as _time

        prefs, metrics = dbs
        engine, venue, _ = _engine(prefs, metrics)
        await _opt_in(prefs)
        pid = await engine.propose_copy(_wallet_sig(), source="w", meta=_meta())
        sig, _, meta = engine._pending_copies[UID]
        engine._pending_copies[UID] = (sig, _time.time() - 1, meta)
        assert await engine.confirm_copy(UID) is False
        row = await metrics.get_copy_trade(pid, UID)
        assert row.status == "expired"


class TestWatcherPhase4:
    def _watcher(self, db, *, states, venue=None):
        from src.trading.wallet_watcher import WalletWatcher
        from src.trading.hl_info_client import WalletPosition, WalletState

        info = AsyncMock()
        seq = list(states)

        async def _next_state(addr):
            return seq.pop(0) if len(seq) > 1 else seq[0]

        info.get_clearinghouse_state = AsyncMock(side_effect=_next_state)
        info.get_candles = AsyncMock(return_value=[
            {"t": i, "o": "25", "h": "26", "l": "24", "c": "25"}
            for i in range(30)
        ])
        engine = AsyncMock()
        engine.propose_copy = AsyncMock(return_value=777)
        blofin = AsyncMock()
        blofin.resolve_inst_id = AsyncMock(
            return_value=SimpleNamespace(inst_id="HYPE-USDT"),
        )
        dms: list[str] = []

        async def _dm(uid, text):
            dms.append(text)

        watcher = WalletWatcher(
            info_client=info, metrics_db=db, engine=engine,
            blofin_client=blofin, config=WalletCopyConfig(),
            send_dm=_dm, allowlist=frozenset({UID}), venue=venue,
        )
        return watcher, engine, dms

    @staticmethod
    def _state(*positions, account=10_000.0):
        from src.trading.hl_info_client import WalletState

        return WalletState(
            account_value=account, positions={p.coin: p for p in positions},
        )

    @staticmethod
    def _pos(coin="HYPE", szi=100.0, margin=1000.0):
        from src.trading.hl_info_client import WalletPosition

        return WalletPosition(coin=coin, szi=szi, entry_px=25.0, leverage=10,
                              notional=2500.0, margin_used=margin)

    @pytest_asyncio.fixture
    async def db(self, tmp_path: Path):
        d = WalletMetricsDB(db_path=str(tmp_path / "scout.db"))
        await d.open()
        await d.save_tracked_wallet(
            TrackedWallet(address=LEADER, status="tracked"),
        )
        yield d
        await d.close()

    @pytest.mark.asyncio
    async def test_open_passes_meta_with_gate_context(self, db):
        watcher, engine, _ = self._watcher(
            db, states=[self._state(), self._state(self._pos())],
        )
        await watcher.poll_once()
        await watcher.poll_once()
        kw = engine.propose_copy.await_args.kwargs
        meta = kw["meta"]
        assert meta["leader_address"] == LEADER
        assert meta["inst_id"] == "HYPE-USDT"
        assert meta["proposal_price"] == pytest.approx(25.0)
        assert meta["atr"] is not None
        assert meta["max_deviation_atr"] == pytest.approx(0.6)

    @pytest.mark.asyncio
    async def test_skips_are_shadow_logged(self, db):
        watcher, engine, _ = self._watcher(
            db,
            states=[
                self._state(),
                self._state(self._pos(margin=10.0)),   # dust conviction
            ],
        )
        await watcher.poll_once()
        await watcher.poll_once()
        engine.propose_copy.assert_not_called()
        cur = await db._conn.execute(
            "SELECT kind FROM watcher_events WHERE kind LIKE 'skip_%'",
        )
        kinds = [r["kind"] for r in await cur.fetchall()]
        assert "skip_conviction" in kinds

    @pytest.mark.asyncio
    async def test_exit_dm_names_linked_position(self, db):
        await db.insert_copy_trade(
            leader_address=LEADER, coin="HYPE", inst_id="HYPE-USDT",
            telegram_user_id=UID, side="LONG", proposal_id=888,
            proposal_price=25.0, atr_at_proposal=2.0,
        )
        await db.mark_copy_trade_filled(
            888, UID, order_ref="ord-9", size_base=40.0, leverage=10,
        )
        watcher, _, dms = self._watcher(
            db, states=[self._state(self._pos()), self._state()],
        )
        await watcher.poll_once()   # baseline with open position
        await watcher.poll_once()   # close
        closed = [t for t in dms if "CLOSED" in t]
        assert closed and "ord-9" in closed[0]

    @pytest.mark.asyncio
    async def test_reconciler_marks_vanished_position(self, db):
        await db.insert_copy_trade(
            leader_address=LEADER, coin="HYPE", inst_id="HYPE-USDT",
            telegram_user_id=UID, side="LONG", proposal_id=889,
            proposal_price=25.0, atr_at_proposal=2.0,
        )
        await db.mark_copy_trade_filled(
            889, UID, order_ref="ord-10", size_base=40.0, leverage=10,
        )
        venue = AsyncMock()
        venue.get_open_position = AsyncMock(return_value=0.0)
        watcher, _, dms = self._watcher(
            db, states=[self._state()], venue=venue,
        )
        watcher._polls_since_reconcile = 19   # trigger on this poll
        await watcher.poll_once()
        assert await db.open_copy_trades(LEADER) == []
        row = await db.get_copy_trade(889, UID)
        assert row.status == "closed_unreconciled"
        assert await db.leader_realized_pnl(LEADER) == 0.0
        assert any("closed_unreconciled" in t for t in dms)
