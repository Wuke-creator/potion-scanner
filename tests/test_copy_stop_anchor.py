"""The copy stop-loss must be anchored to OUR fill, not the leader's entry.

The wallet watcher derives a stop and a 1R/2R/3R ladder from the tracked
wallet's Hyperliquid entry price. We fill at market on Blofin up to 15 minutes
later. Passing those levels straight through put the stop a fixed distance from
a price we never paid, and the confirm-time deviation gate only looked at
ADVERSE moves, so a move in our favour silently compressed the stop below the
intended ATR multiple and, past one atr_mult, put it on the wrong side of the
fill entirely: Blofin then either triggers it immediately or rejects it and
leaves the position with no stop at all.

These tests pin the fix: levels re-derived from plan.price, a wrong-side stop
refused outright, and channel calls (which carry the caller's own levels)
left untouched.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.config.settings import AutotradeConfig
from src.trading.autotrade_engine import AutotradeEngine
from src.trading.autotrade_prefs_db import AutotradePrefsDB
from src.trading.hyperliquid_client import AccountSnapshot
from src.trading.venue import VenueConnection, VenuePlan, VenueResult

UID = 111
RISK = 1000.0          # the ATR x atr_mult distance the stop must keep


def _plan(price: float, is_long: bool = True) -> VenuePlan:
    return VenuePlan(coin="BTC", is_long=is_long, price=price, size=0.02,
                     leverage=10, notional_usd=1000.0)


@pytest_asyncio.fixture
async def prefs_db(tmp_path: Path):
    db = AutotradePrefsDB(db_path=str(tmp_path / "prefs.db"))
    await db.open()
    yield db
    await db.close()


def _make_engine(prefs_db, *, plan, dry_run=False, copy_store=None):
    cfg = AutotradeConfig(
        enabled=True, dry_run=dry_run, venue="blofin", network="mainnet",
        allowlist=frozenset({UID}), default_size_pct=5.0, max_leverage=20,
        max_per_day=10, min_collateral_usdc=5.0, slippage_bps=100,
        risk_enabled=True,
    )
    venue = AsyncMock()
    venue.name = "blofin"
    venue.supports_risk_guard = True
    venue.get_connection = AsyncMock(
        return_value=VenueConnection(is_active=True, label="Blofin API key"),
    )
    venue.get_balance = AsyncMock(return_value=1000.0)
    venue.get_account_snapshot = AsyncMock(
        return_value=AccountSnapshot(account_value=100_000.0, positions={}),
    )
    venue.plan = AsyncMock(return_value=plan)
    # the real adapter stamps entry_price with plan.price (venue.py), so the
    # attribution row records what we actually filled at
    venue.place = AsyncMock(return_value=VenueResult(
        coin="BTC", size=0.02, sl_ok=True, tp_ok=True, ref="1", tp_legs=[],
        entry_price=plan.price if plan else 0.0,
    ))
    venue.mark_success = AsyncMock()
    venue.mark_failure = AsyncMock()
    venue.get_price = AsyncMock(return_value=plan.price if plan else 0.0)

    verification = AsyncMock()
    verification.get_verified = AsyncMock(
        return_value=SimpleNamespace(is_active=True),
    )
    send_dm = AsyncMock()
    engine = AutotradeEngine(
        config=cfg, max_collateral_usdc=5000.0, venue=venue,
        prefs_db=prefs_db, verification_db=verification, send_dm=send_dm,
    )
    if copy_store is not None:
        engine.attach_copy_store(copy_store)
    return engine, venue, send_dm


async def _opt_in(prefs_db, uid=UID):
    await prefs_db.set_enabled(uid, True)
    await prefs_db.accept_disclosure(uid)


def _copy_signal(**kw):
    """A wallet copy as the engine builds it: levels from the LEADER's entry of
    50000, plus the raw ATR distance that lets the fire path re-anchor them."""
    base = dict(id=1, pair="BTC/USDT", side="LONG", leverage=10,
                tp1=51000.0, tp2=52000.0, tp3=53000.0, stop_loss=49000.0,
                copy_risk_per_unit=RISK)
    base.update(kw)
    return SimpleNamespace(**base)


class TestReanchor:
    @pytest.mark.asyncio
    async def test_favourable_drift_no_longer_strands_the_stop(self, prefs_db):
        """THE BUG. Long proposed at 50000 with stop 49000. Price drifts DOWN to
        48500 before confirm, which is favourable for a buyer and therefore
        invisible to the one-sided deviation gate. The old code sent stop=49000,
        ABOVE our fill."""
        engine, venue, _ = _make_engine(prefs_db, plan=_plan(48500.0))
        await _opt_in(prefs_db)
        await engine.on_new_signal(_copy_signal())
        venue.place.assert_awaited_once()
        kw = venue.place.await_args.kwargs
        assert kw["stop_loss"] == pytest.approx(47500.0)      # 48500 - 1000
        assert kw["stop_loss"] < 48500.0, "stop must sit below a long entry"
        assert kw["take_profits"] == pytest.approx([49500.0, 50500.0, 51500.0])

    @pytest.mark.asyncio
    async def test_adverse_drift_keeps_risk_at_one_atr_multiple(self, prefs_db):
        """A move against us inside the gate's tolerance used to leave the stop
        further than atr_mult from the fill. Risk must stay exactly RISK."""
        engine, venue, _ = _make_engine(prefs_db, plan=_plan(50800.0))
        await _opt_in(prefs_db)
        await engine.on_new_signal(_copy_signal())
        kw = venue.place.await_args.kwargs
        assert kw["stop_loss"] == pytest.approx(49800.0)      # 50800 - 1000
        assert 50800.0 - kw["stop_loss"] == pytest.approx(RISK)

    @pytest.mark.asyncio
    async def test_short_side_anchors_above_the_fill(self, prefs_db):
        """The fill price must differ from the leader's entry, or re-anchoring and
        passing the levels through are numerically identical and the test proves
        nothing. Leader shorted at 50000 (stop 51000); we fill at 51500."""
        engine, venue, _ = _make_engine(
            prefs_db, plan=_plan(51500.0, is_long=False),
        )
        await _opt_in(prefs_db)
        await engine.on_new_signal(_copy_signal(
            side="SHORT", stop_loss=51000.0,
            tp1=49000.0, tp2=48000.0, tp3=47000.0,
        ))
        kw = venue.place.await_args.kwargs
        assert kw["stop_loss"] == pytest.approx(52500.0)      # 51500 + 1000
        assert kw["stop_loss"] > 51500.0, "stop must sit above a short entry"
        assert kw["take_profits"] == pytest.approx([50500.0, 49500.0, 48500.0])

    @pytest.mark.asyncio
    async def test_channel_calls_pass_through_untouched(self, prefs_db):
        """Cabal calls carry the caller's own levels and no risk distance. They
        must NOT be re-derived: those levels are the author's, not ours."""
        engine, venue, _ = _make_engine(prefs_db, plan=_plan(48500.0))
        await _opt_in(prefs_db)
        sig = _copy_signal()
        del sig.copy_risk_per_unit
        sig.stop_loss = 47000.0                    # already below the fill
        await engine.on_new_signal(sig)
        kw = venue.place.await_args.kwargs
        assert kw["stop_loss"] == pytest.approx(47000.0)
        assert kw["take_profits"] == pytest.approx([51000.0, 52000.0, 53000.0])


class TestWrongSideGuard:
    @pytest.mark.asyncio
    async def test_stop_above_a_long_entry_is_refused(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, plan=_plan(48500.0))
        await _opt_in(prefs_db)
        sig = _copy_signal()
        del sig.copy_risk_per_unit                 # no re-anchor to save it
        await engine.on_new_signal(sig)            # stop 49000 > price 48500
        venue.place.assert_not_called()
        assert "wrong side" in send_dm.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_stop_below_a_short_entry_is_refused(self, prefs_db):
        engine, venue, send_dm = _make_engine(
            prefs_db, plan=_plan(50000.0, is_long=False),
        )
        await _opt_in(prefs_db)
        sig = _copy_signal(side="SHORT", stop_loss=49000.0)
        del sig.copy_risk_per_unit
        await engine.on_new_signal(sig)
        venue.place.assert_not_called()
        assert "wrong side" in send_dm.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_volatility_wider_than_price_is_refused(self, prefs_db):
        """An ATR distance exceeding the price leaves no placeable stop. Refuse
        rather than send a non-positive one and end up unprotected."""
        engine, venue, send_dm = _make_engine(prefs_db, plan=_plan(900.0))
        await _opt_in(prefs_db)
        await engine.on_new_signal(_copy_signal())   # risk 1000 > price 900
        venue.place.assert_not_called()
        assert "volatility" in send_dm.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_volatility_wider_than_price_is_refused_on_shorts_too(self, prefs_db):
        """The long check (stop <= 0) can never fire on a short, because
        price + risk is always positive. A short with risk >= price would place a
        live order with an unreachable stop and an empty ladder, since every TP
        prices below zero and is filtered away."""
        engine, venue, send_dm = _make_engine(
            prefs_db, plan=_plan(900.0, is_long=False),
        )
        await _opt_in(prefs_db)
        await engine.on_new_signal(_copy_signal(side="SHORT", stop_loss=1900.0))
        venue.place.assert_not_called()
        assert "volatility" in send_dm.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_a_refusal_releases_the_fire_claim(self, prefs_db):
        """The dedupe claim must not swallow the signal id, or a corrected
        re-proposal of the same trade would be silently ignored."""
        engine, venue, _ = _make_engine(prefs_db, plan=_plan(48500.0))
        await _opt_in(prefs_db)
        sig = _copy_signal()
        del sig.copy_risk_per_unit
        await engine.on_new_signal(sig)
        venue.place.assert_not_called()
        assert await prefs_db.try_claim_fire(UID, 1) is True


class TestAttribution:
    @pytest.mark.asyncio
    async def test_recorded_stop_is_the_one_actually_placed(self, prefs_db):
        """open_copy_heat_usd measures abs(entry - stop). If the row keeps the
        proposal-time stop, the heat cap guards a level we never placed."""
        store = AsyncMock()
        engine, venue, _ = _make_engine(
            prefs_db, plan=_plan(48500.0), copy_store=store,
        )
        await _opt_in(prefs_db)
        sig = SimpleNamespace(
            pair="BTC/USDT", side="LONG", leverage=10,
            take_profits=[51000.0, 52000.0, 53000.0], stop_loss=49000.0,
            size_pct_override=10.0, risk_per_unit=RISK, note="",
        )
        meta = {"leader_address": "0xabc", "coin": "BTC",
                "inst_id": "BTC-USDT", "proposal_price": 50000.0,
                "atr": None, "max_deviation_atr": 0.6}
        await engine.propose_copy(sig, source="wallet 0xabc", meta=meta)
        assert await engine.confirm_copy(UID) is True

        assert venue.place.await_args.kwargs["stop_loss"] == pytest.approx(47500.0)
        filled = store.mark_copy_trade_filled.await_args.kwargs
        assert filled["stop_price"] == pytest.approx(47500.0)
        assert filled["entry_price"] == pytest.approx(48500.0)

    @pytest.mark.asyncio
    async def test_a_rejected_stop_is_recorded_as_no_stop(self, prefs_db):
        """When Blofin refuses the SL the position is unprotected. Recording the
        requested level anyway would let open_copy_heat_usd count it as bounded
        risk and keep waving further copies through the heat cap."""
        store = AsyncMock()
        engine, venue, _ = _make_engine(
            prefs_db, plan=_plan(48500.0), copy_store=store,
        )
        venue.place = AsyncMock(return_value=VenueResult(
            coin="BTC", size=0.02, sl_ok=False, tp_ok=True, ref="1", tp_legs=[],
            entry_price=48500.0,
        ))
        await _opt_in(prefs_db)
        sig = SimpleNamespace(
            pair="BTC/USDT", side="LONG", leverage=10,
            take_profits=[51000.0], stop_loss=49000.0,
            size_pct_override=10.0, risk_per_unit=RISK, note="",
        )
        await engine.propose_copy(sig, source="wallet 0xabc",
                                  meta={"leader_address": "0xabc", "coin": "BTC",
                                        "inst_id": "BTC-USDT", "proposal_price": 50000.0,
                                        "atr": None, "max_deviation_atr": 0.6})
        assert await engine.confirm_copy(UID) is True
        assert store.mark_copy_trade_filled.await_args.kwargs["stop_price"] is None

    @pytest.mark.asyncio
    async def test_propose_copy_carries_the_risk_distance(self, prefs_db):
        """The watcher's risk_per_unit must survive onto the synthetic proposal,
        or the fire path has nothing to re-anchor with."""
        engine, _venue, _ = _make_engine(
            prefs_db, plan=_plan(50000.0), dry_run=True,
        )
        await _opt_in(prefs_db)
        sig = SimpleNamespace(
            pair="BTC/USDT", side="LONG", leverage=10,
            take_profits=[51000.0], stop_loss=49000.0,
            size_pct_override=10.0, risk_per_unit=RISK, note="",
        )
        await engine.propose_copy(sig, source="wallet 0xabc", meta={"coin": "BTC"})
        pending, _expires, _meta = engine._pending_copies[UID]
        assert pending.copy_risk_per_unit == pytest.approx(RISK)
