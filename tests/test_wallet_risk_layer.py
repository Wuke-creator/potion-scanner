"""Phase 5 tests: auto-mirror exits, per-leader cumulative stop, daily
circuit breaker, heat/direction caps, volume clamp, funding gate,
risk-budget sizing, and the BlofinVenue snapshot/close additions."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.config.settings import WalletCopyConfig
from src.trading.hl_info_client import WalletPosition, WalletState
from src.trading.venue import BlofinVenue, VenueError, VenueResult
from src.trading.wallet_metrics_db import (
    TrackedWallet,
    WalletMetrics,
    WalletMetricsDB,
)
from src.trading.wallet_watcher import WalletWatcher

UID = 99
LEADER = "0xadd12adbbd5db87674b38af99b6dd34dd2a45e0d"


def _pos(coin="HYPE", szi=100.0, margin=1000.0):
    return WalletPosition(coin=coin, szi=szi, entry_px=25.0, leverage=10,
                          notional=2500.0, margin_used=margin)


def _state(*positions, account=10_000.0):
    return WalletState(account_value=account,
                       positions={p.coin: p for p in positions})


def _candles_ok(n=30):
    return [{"t": i, "o": "25", "h": "26", "l": "24", "c": "25"}
            for i in range(n)]


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    d = WalletMetricsDB(db_path=str(tmp_path / "scout.db"))
    await d.open()
    await d.save_tracked_wallet(TrackedWallet(
        address=LEADER, status="tracked", promoted_at=0,
    ))
    yield d
    await d.close()


def _watcher(db, *, states, cfg=None, venue=None, funding_rate=None):
    info = AsyncMock()
    seq = list(states)

    async def _next_state(addr):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    info.get_clearinghouse_state = AsyncMock(side_effect=_next_state)
    info.get_candles = AsyncMock(return_value=_candles_ok())
    engine = AsyncMock()
    engine.propose_copy = AsyncMock(return_value=1234)
    blofin = AsyncMock()
    blofin.resolve_inst_id = AsyncMock(
        return_value=SimpleNamespace(inst_id="HYPE-USDT"),
    )
    blofin.get_funding_rate = AsyncMock(return_value=funding_rate)
    dms: list[str] = []

    async def _dm(uid, text):
        dms.append(text)

    watcher = WalletWatcher(
        info_client=info, metrics_db=db, engine=engine,
        blofin_client=blofin, config=cfg or WalletCopyConfig(),
        send_dm=_dm, allowlist=frozenset({UID}), venue=venue,
    )
    return watcher, engine, dms


async def _filled_copy(db, *, proposal_id, coin="HYPE", side="LONG",
                       entry=25.0, stop=22.0, size=40.0):
    await db.insert_copy_trade(
        leader_address=LEADER, coin=coin, inst_id=f"{coin}-USDT",
        telegram_user_id=UID, side=side, proposal_id=proposal_id,
        proposal_price=entry, atr_at_proposal=2.0, stop_price=stop,
    )
    await db.mark_copy_trade_filled(
        proposal_id, UID, order_ref=f"ord-{proposal_id}", size_base=size,
        leverage=10, entry_price=entry,
    )


class TestMirrorExits:
    @pytest.mark.asyncio
    async def test_flag_off_only_dms(self, db):
        await _filled_copy(db, proposal_id=1)
        venue = AsyncMock()
        watcher, _, dms = _watcher(
            db, states=[_state(_pos()), _state()], venue=venue,
        )
        await watcher.poll_once()
        await watcher.poll_once()
        venue.close_position.assert_not_called()
        assert any("CLOSED" in t for t in dms)

    @pytest.mark.asyncio
    async def test_flag_on_closes_and_records_pnl(self, db):
        await _filled_copy(db, proposal_id=2, entry=25.0, size=40.0)
        venue = AsyncMock()
        venue.close_position = AsyncMock(return_value=VenueResult(
            coin="HYPE", size=40.0, ref="close-1", entry_price=26.0,
        ))
        watcher, _, dms = _watcher(
            db, states=[_state(_pos()), _state()],
            cfg=WalletCopyConfig(mirror_exits=True), venue=venue,
        )
        await watcher.poll_once()
        await watcher.poll_once()
        venue.close_position.assert_awaited_once()
        assert venue.close_position.await_args.kwargs["fraction"] == 1.0
        assert await db.open_copy_trades(LEADER) == []
        # est pnl: (26-25) * 40 = +40
        assert await db.leader_realized_pnl(LEADER) == pytest.approx(40.0)
        assert any("Mirror exit" in t for t in dms)

    @pytest.mark.asyncio
    async def test_close_failure_dms_urgently(self, db):
        await _filled_copy(db, proposal_id=3)
        venue = AsyncMock()
        venue.close_position = AsyncMock(side_effect=VenueError("rejected"))
        watcher, _, dms = _watcher(
            db, states=[_state(_pos()), _state()],
            cfg=WalletCopyConfig(mirror_exits=True), venue=venue,
        )
        await watcher.poll_once()
        await watcher.poll_once()
        assert any("MIRROR CLOSE FAILED" in t for t in dms)
        # row stays open so the reconciler / manual close can handle it
        assert len(await db.open_copy_trades(LEADER)) == 1

    @pytest.mark.asyncio
    async def test_flip_mirrors_out_old_side(self, db):
        await _filled_copy(db, proposal_id=4)
        venue = AsyncMock()
        venue.close_position = AsyncMock(return_value=VenueResult(
            coin="HYPE", size=40.0, ref="close-2", entry_price=24.0,
        ))
        watcher, engine, dms = _watcher(
            db,
            states=[_state(_pos()), _state(_pos(szi=-50.0, margin=1000.0))],
            cfg=WalletCopyConfig(mirror_exits=True), venue=venue,
        )
        await watcher.poll_once()
        await watcher.poll_once()
        venue.close_position.assert_awaited_once()   # old long closed
        engine.propose_copy.assert_awaited_once()    # new short proposed


class TestLeaderStopAndWindDown:
    @pytest.mark.asyncio
    async def test_breach_untracks_and_dms(self, db):
        await _filled_copy(db, proposal_id=5, entry=25.0, size=40.0)
        venue = AsyncMock()
        # close at 20: est pnl (20-25)*40 = -200
        venue.close_position = AsyncMock(return_value=VenueResult(
            coin="HYPE", size=40.0, ref="close-3", entry_price=20.0,
        ))
        watcher, _, dms = _watcher(
            db, states=[_state(_pos()), _state()],
            cfg=WalletCopyConfig(mirror_exits=True, leader_stop_usd=150.0),
            venue=venue,
        )
        await watcher.poll_once()
        await watcher.poll_once()
        tw = await db.get_tracked_wallet(LEADER)
        assert tw.status == "candidate"
        assert any("LEADER STOP" in t for t in dms)

    @pytest.mark.asyncio
    async def test_untracked_leader_with_open_copy_still_polled(self, db):
        # demote the leader but leave a filled copy open
        await _filled_copy(db, proposal_id=6)
        tw = await db.get_tracked_wallet(LEADER)
        tw.status = "candidate"
        await db.save_tracked_wallet(tw)
        watcher, engine, dms = _watcher(
            db, states=[_state(_pos()), _state(_pos(), _pos(coin="ZEC", szi=5.0))],
        )
        await watcher.poll_once()   # baselines despite untracked
        await watcher.poll_once()   # ZEC open appears
        # polled (deltas seen) but proposals refused for untracked leaders
        engine.propose_copy.assert_not_called()
        cur = await db._conn.execute(
            "SELECT COUNT(*) AS n FROM watcher_events WHERE kind='skip_untracked'",
        )
        assert (await cur.fetchone())["n"] == 1


class TestProposalRiskGates:
    @pytest.mark.asyncio
    async def test_daily_breaker_suppresses_proposals(self, db):
        # a closed copy with a big loss today
        tid = await db.insert_copy_trade(
            leader_address=LEADER, coin="OLD", inst_id="OLD-USDT",
            telegram_user_id=UID, side="LONG", proposal_id=7,
            proposal_price=1.0, atr_at_proposal=0.1,
        )
        await db.mark_copy_trade_filled(7, UID, order_ref="o", size_base=1.0,
                                        leverage=1, entry_price=1.0)
        await db.close_copy_trade(tid, close_reason="sl", realized_pnl=-500.0)
        watcher, engine, _ = _watcher(
            db, states=[_state(), _state(_pos())],
            cfg=WalletCopyConfig(daily_loss_stop_usd=300.0),
        )
        await watcher.poll_once()
        await watcher.poll_once()
        engine.propose_copy.assert_not_called()

    @pytest.mark.asyncio
    async def test_direction_cluster_cap(self, db):
        for i in range(3):
            await _filled_copy(db, proposal_id=10 + i, coin=f"C{i}")
        watcher, engine, _ = _watcher(
            db, states=[_state(), _state(_pos())],
            cfg=WalletCopyConfig(max_same_direction=3),
        )
        await watcher.poll_once()
        await watcher.poll_once()
        engine.propose_copy.assert_not_called()

    @pytest.mark.asyncio
    async def test_heat_cap_blocks_when_open_risk_high(self, db):
        # open risk: |25-22|*40 = $120; balance 1000 at 6% cap = $60
        await _filled_copy(db, proposal_id=20)
        venue = AsyncMock()
        venue.get_balance = AsyncMock(return_value=1000.0)
        watcher, engine, _ = _watcher(
            db, states=[_state(), _state(_pos(coin="ZEC", szi=10.0))],
            cfg=WalletCopyConfig(heat_cap_pct=6.0), venue=venue,
        )
        watcher._blofin.resolve_inst_id = AsyncMock(
            return_value=SimpleNamespace(inst_id="ZEC-USDT"),
        )
        await watcher.poll_once()
        await watcher.poll_once()
        engine.propose_copy.assert_not_called()

    @pytest.mark.asyncio
    async def test_volume_clamp_caps_outsized_punt(self, db):
        # history: leader normally proposes ~2% sizing
        for i in range(3):
            await db.log_event(LEADER, "HYPE", "proposed", {"size_pct": 2.0})
        watcher, engine, _ = _watcher(
            db,
            states=[_state(account=10_000.0),
                    _state(_pos(margin=5000.0), account=10_000.0)],  # 50% punt
            cfg=WalletCopyConfig(volume_clamp_mult=3.0),
        )
        await watcher.poll_once()
        await watcher.poll_once()
        sig = engine.propose_copy.await_args.args[0]
        assert sig.size_pct_override == pytest.approx(6.0)   # 3x their 2% avg
        assert "clamped" in sig.note

    @pytest.mark.asyncio
    async def test_funding_gate_halves_then_skips(self, db):
        # risk_frac = ATR(2)*1.5/25 = 0.12; threshold = 0.25*0.12 = 0.03
        # hold default 480min = 1 funding period. rate 0.04 -> halve;
        # rate 0.07 > 2x threshold -> skip.
        watcher, engine, _ = _watcher(
            db, states=[_state(), _state(_pos())], funding_rate=0.04,
        )
        await watcher.poll_once()
        await watcher.poll_once()
        sig = engine.propose_copy.await_args.args[0]
        assert sig.size_pct_override == pytest.approx(5.0)   # 10% halved
        assert "halved" in sig.note

        watcher2, engine2, _ = _watcher(
            db, states=[_state(), _state(_pos(coin="ZEC"))], funding_rate=0.07,
        )
        watcher2._blofin.resolve_inst_id = AsyncMock(
            return_value=SimpleNamespace(inst_id="ZEC-USDT"),
        )
        await watcher2.poll_once()
        await watcher2.poll_once()
        engine2.propose_copy.assert_not_called()

    @pytest.mark.asyncio
    async def test_risk_budget_sizing(self, db):
        # stop_frac = 3/25 = 0.12, lev = 10, budget 1% * conv_mult 1.0
        # (conviction 0.10) -> pct = 1.0 / (10*0.12) = 0.83%
        watcher, engine, _ = _watcher(
            db, states=[_state(), _state(_pos(margin=1000.0))],
            cfg=WalletCopyConfig(risk_budget_pct=1.0, volume_clamp_mult=0),
        )
        await watcher.poll_once()
        await watcher.poll_once()
        sig = engine.propose_copy.await_args.args[0]
        assert sig.size_pct_override == pytest.approx(0.83, abs=0.01)
        assert "risk budget" in sig.note

    @pytest.mark.asyncio
    async def test_kelly_cap_shrinks_budget(self, db):
        # weak wallet stats -> small kelly -> budget capped below 1%
        await db.upsert_metrics(WalletMetrics(
            address=LEADER, snapshot_date="2026-07-11",
            win_rate=0.55, profit_factor=1.3, median_hold_min=480.0,
        ))
        watcher, engine, _ = _watcher(
            db, states=[_state(), _state(_pos(margin=1000.0))],
            cfg=WalletCopyConfig(risk_budget_pct=5.0, volume_clamp_mult=0),
        )
        await watcher.poll_once()
        await watcher.poll_once()
        sig = engine.propose_copy.await_args.args[0]
        # kelly f = 0.55 - 0.45/(1.3*0.45/0.55) ~= 0.127; cap 0.25*f*100 ~= 3.18%
        # pct = 3.18 / (10*0.12) ~= 2.65 < uncapped 4.17
        assert sig.size_pct_override < 4.0


class TestBlofinVenueAdditions:
    def _venue(self):
        client = AsyncMock()
        creds_db = AsyncMock()
        creds_db.get_creds = AsyncMock(return_value=object())
        venue = BlofinVenue(client, creds_db)
        return venue, client

    @pytest.mark.asyncio
    async def test_close_position_reduce_only_floors_to_lot(self):
        venue, client = self._venue()
        client.resolve_inst_id = AsyncMock(return_value=SimpleNamespace(
            inst_id="HYPE-USDT", lot_size=Decimal("1"),
            contract_value=Decimal("0.1"), min_size=Decimal("1"),
            max_leverage=50,
        ))
        client.get_position = AsyncMock(return_value=Decimal("10"))
        client.close_position_market = AsyncMock(
            return_value=SimpleNamespace(order_id="c-1", raw={}),
        )
        client.get_last_price = AsyncMock(return_value=25.0)
        res = await venue.close_position(UID, pair="HYPE/USDT", fraction=0.55)
        kw = client.close_position_market.await_args.kwargs
        assert kw["side"] == "sell"
        assert kw["size_contracts"] == Decimal("5")   # floor(5.5)
        assert res.ref == "c-1"

    @pytest.mark.asyncio
    async def test_close_position_full_never_leaves_dust(self):
        venue, client = self._venue()
        client.resolve_inst_id = AsyncMock(return_value=SimpleNamespace(
            inst_id="Z-USDT", lot_size=Decimal("1"),
            contract_value=Decimal("1"), min_size=Decimal("1"),
            max_leverage=50,
        ))
        client.get_position = AsyncMock(return_value=Decimal("-7"))
        client.close_position_market = AsyncMock(
            return_value=SimpleNamespace(order_id="c-2", raw={}),
        )
        client.get_last_price = AsyncMock(return_value=1.0)
        await venue.close_position(UID, pair="Z/USDT", fraction=1.0)
        kw = client.close_position_market.await_args.kwargs
        assert kw["side"] == "buy"                    # closing a short
        assert kw["size_contracts"] == Decimal("7")

    @pytest.mark.asyncio
    async def test_close_position_flat_raises(self):
        venue, client = self._venue()
        client.resolve_inst_id = AsyncMock(return_value=SimpleNamespace(
            inst_id="Z-USDT", lot_size=Decimal("1"),
            contract_value=Decimal("1"), min_size=Decimal("1"),
            max_leverage=50,
        ))
        client.get_position = AsyncMock(return_value=Decimal("0"))
        with pytest.raises(VenueError):
            await venue.close_position(UID, pair="Z/USDT", fraction=1.0)

    @pytest.mark.asyncio
    async def test_account_snapshot_sums_equity_and_notional(self):
        venue, client = self._venue()
        client.get_available_usdt = AsyncMock(return_value=500.0)
        client.get_all_positions = AsyncMock(return_value=[
            {"instId": "HYPE-USDT", "positions": "100", "margin": "250",
             "unrealizedPnl": "50", "markPrice": "25", "leverage": "10"},
        ])
        client.resolve_inst_id = AsyncMock(return_value=SimpleNamespace(
            inst_id="HYPE-USDT", lot_size=Decimal("1"),
            contract_value=Decimal("0.1"), min_size=Decimal("1"),
            max_leverage=50,
        ))
        snap = await venue.get_account_snapshot(UID)
        assert snap.account_value == pytest.approx(800.0)
        assert snap.positions["HYPE"] == pytest.approx(100 * 0.1 * 25)

    def test_supports_risk_guard_now_true(self):
        venue, _ = self._venue()
        assert venue.supports_risk_guard is True
