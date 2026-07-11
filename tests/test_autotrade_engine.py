"""Tests for the autotrade engine loop.

Uses a real AutotradePrefsDB (tmp) so dedupe + daily-cap behaviour is
exercised for real; the venue (Hyperliquid or Blofin adapter) and the
verification store are mocked. The mock venue advertises risk-guard support
and returns a clean account snapshot, so the guard runs and passes exactly
as it does on Hyperliquid.
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
from src.trading.venue import VenueConnection, VenueError, VenuePlan, VenueResult

UID = 111
_DEFAULT_PLAN = VenuePlan(
    coin="BTC", is_long=True, price=50000.0, size=0.02, leverage=10,
    notional_usd=1000.0,
)
_DEFAULT_RESULT = VenueResult(
    coin="BTC", size=0.02, sl_ok=True, tp_ok=True, ref="1",
    tp_legs=[(52000.0, 0.02)],
)
_SENTINEL = object()


@pytest_asyncio.fixture
async def prefs_db(tmp_path: Path):
    db = AutotradePrefsDB(db_path=str(tmp_path / "autotrade_prefs.db"))
    await db.open()
    yield db
    await db.close()


def _signal(**kw):
    base = dict(id=1, pair="BTC/USDT", side="LONG", leverage=10,
                tp1=52000.0, stop_loss=48000.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _make_engine(
    prefs_db, *, enabled=True, dry_run=True, allowlist=frozenset({UID}),
    max_per_day=10, balance=1000.0, plan=_SENTINEL, place_result=None,
    place_error=None, verified=True, connected=True, risk_enabled=True,
    venue_name="hyperliquid",
):
    cfg = AutotradeConfig(
        enabled=enabled, dry_run=dry_run, venue=venue_name, network="testnet",
        allowlist=allowlist, default_size_pct=5.0, max_leverage=20,
        max_per_day=max_per_day, min_collateral_usdc=5.0, slippage_bps=100,
        risk_enabled=risk_enabled,
    )
    venue = AsyncMock()
    venue.name = venue_name
    venue.supports_risk_guard = True
    venue.get_connection = AsyncMock(return_value=(
        VenueConnection(is_active=True, label="0xMASTER") if connected else None
    ))
    venue.get_balance = AsyncMock(return_value=balance)
    # Clean account for the risk guard: comfortable value, no open positions.
    venue.get_account_snapshot = AsyncMock(
        return_value=AccountSnapshot(account_value=100_000.0, positions={}),
    )
    venue.plan = AsyncMock(
        return_value=_DEFAULT_PLAN if plan is _SENTINEL else plan,
    )
    if place_error is not None:
        venue.place = AsyncMock(side_effect=place_error)
    else:
        venue.place = AsyncMock(return_value=place_result or _DEFAULT_RESULT)
    venue.mark_success = AsyncMock()
    venue.mark_failure = AsyncMock()

    verification = AsyncMock()
    verification.get_verified = AsyncMock(return_value=(
        SimpleNamespace(is_active=True) if verified else None
    ))

    send_dm = AsyncMock()
    engine = AutotradeEngine(
        config=cfg, max_collateral_usdc=5000.0, venue=venue,
        prefs_db=prefs_db, verification_db=verification, send_dm=send_dm,
    )
    return engine, venue, send_dm


async def _opt_in(prefs_db, uid=UID):
    await prefs_db.set_enabled(uid, True)
    await prefs_db.accept_disclosure(uid)


class TestGates:
    @pytest.mark.asyncio
    async def test_disabled_does_nothing(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, enabled=False)
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal())
        venue.plan.assert_not_called()
        send_dm.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_allowlist_does_nothing(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, allowlist=frozenset())
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal())
        venue.plan.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_elite_skipped(self, prefs_db):
        engine, venue, _ = _make_engine(prefs_db, verified=False)
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal())
        venue.plan.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_connected_skipped(self, prefs_db):
        engine, venue, _ = _make_engine(prefs_db, connected=False)
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal())
        venue.plan.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_opted_in_skipped(self, prefs_db):
        engine, venue, _ = _make_engine(prefs_db)
        # connected but no disclosure -> not ready
        await prefs_db.set_enabled(UID, True)
        await engine.on_new_signal(_signal())
        venue.plan.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_direction_skipped(self, prefs_db):
        engine, venue, _ = _make_engine(prefs_db)
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal(side=None))
        venue.plan.assert_not_called()


class TestDryRun:
    @pytest.mark.asyncio
    async def test_previews_without_placing(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, dry_run=True)
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal())
        venue.place.assert_not_called()
        venue.mark_success.assert_not_called()
        assert send_dm.await_count == 1
        assert "[DRY RUN]" in send_dm.await_args.args[1]
        # dry-run must not consume a daily slot
        prefs = await prefs_db.get_or_default(UID)
        assert prefs.trades_today == 0

    @pytest.mark.asyncio
    async def test_dedupe_same_signal(self, prefs_db):
        engine, _, send_dm = _make_engine(prefs_db, dry_run=True)
        await _opt_in(prefs_db)
        sig = _signal(id=42)
        await engine.on_new_signal(sig)
        await engine.on_new_signal(sig)  # re-broadcast
        assert send_dm.await_count == 1  # second claim fails


class TestLive:
    @pytest.mark.asyncio
    async def test_places_and_reports(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, dry_run=False)
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal())
        venue.place.assert_awaited_once()
        # sized off balance*pct: 1000 * 5% = $50 collateral requested
        assert venue.plan.await_args.kwargs["collateral_usdc"] == pytest.approx(50.0)
        venue.mark_success.assert_awaited_once()
        assert "filled" in send_dm.await_args.args[1].lower()
        prefs = await prefs_db.get_or_default(UID)
        assert prefs.trades_today == 1

    @pytest.mark.asyncio
    async def test_passes_full_tp_ladder(self, prefs_db):
        engine, venue, _ = _make_engine(prefs_db, dry_run=False)
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal(tp1=52000.0, tp2=54000.0, tp3=56000.0))
        assert venue.place.await_args.kwargs["take_profits"] == [52000.0, 54000.0, 56000.0]

    @pytest.mark.asyncio
    async def test_balance_too_low_skips(self, prefs_db):
        engine, venue, send_dm = _make_engine(
            prefs_db, dry_run=False, balance=50.0,  # 5% -> $2.50 < $5 min
        )
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal())
        venue.plan.assert_not_called()
        venue.place.assert_not_called()
        assert "too low" in send_dm.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_unlisted_coin_skips(self, prefs_db):
        engine, venue, send_dm = _make_engine(
            prefs_db, dry_run=False, plan=None,  # venue.plan returns None
        )
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal(pair="OBSCURE/USDT"))
        venue.place.assert_not_called()
        assert "hyperliquid" in send_dm.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_daily_cap_enforced(self, prefs_db):
        engine, venue, send_dm = _make_engine(
            prefs_db, dry_run=False, max_per_day=1,
        )
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal(id=1))
        await engine.on_new_signal(_signal(id=2))
        assert venue.place.await_count == 1
        assert "daily cap" in send_dm.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_place_error_reports_and_releases(self, prefs_db):
        engine, venue, send_dm = _make_engine(
            prefs_db, dry_run=False, place_error=VenueError("insufficient margin"),
        )
        await _opt_in(prefs_db)
        await engine.on_new_signal(_signal(id=7))
        venue.mark_failure.assert_awaited_once()
        assert "failed" in send_dm.await_args.args[1].lower()
        # claim released -> the (user, signal) can be retried
        assert await prefs_db.try_claim_fire(UID, 7) is True


class TestApplyTps:
    @pytest.mark.asyncio
    async def test_places_ladder_and_reports(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, dry_run=False)
        venue.place_tp_ladder = AsyncMock(return_value=VenueResult(
            coin="JUP", size=493.0, sl_ok=True, tp_ok=True,
            tp_legs=[(0.2006, 246.5), (0.194, 147.9), (0.1832, 98.6)],
        ))
        await _opt_in(prefs_db)
        await engine.apply_tps(UID, pair="JUP/USDT",
                               take_profits=[0.2006, 0.194, 0.1832])
        venue.place_tp_ladder.assert_awaited_once()
        assert "TP ladder set" in send_dm.await_args.args[1]

    @pytest.mark.asyncio
    async def test_dry_run_previews_only(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, dry_run=True)
        venue.place_tp_ladder = AsyncMock()
        await _opt_in(prefs_db)
        await engine.apply_tps(UID, pair="JUP/USDT", take_profits=[0.2])
        venue.place_tp_ladder.assert_not_called()
        assert "[DRY RUN]" in send_dm.await_args.args[1]

    @pytest.mark.asyncio
    async def test_venue_error_reported(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, dry_run=False)
        venue.place_tp_ladder = AsyncMock(side_effect=VenueError("no open JUP position"))
        await _opt_in(prefs_db)
        await engine.apply_tps(UID, pair="JUP/USDT", take_profits=[0.2])
        assert "failed" in send_dm.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_not_allowlisted_silent(self, prefs_db):
        engine, venue, send_dm = _make_engine(
            prefs_db, allowlist=frozenset({999}), dry_run=False,
        )
        venue.place_tp_ladder = AsyncMock()
        await _opt_in(prefs_db)
        await engine.apply_tps(UID, pair="JUP/USDT", take_profits=[0.2])
        venue.place_tp_ladder.assert_not_called()
        send_dm.assert_not_called()


class TestCopyProposals:
    def _cabal_sig(self, **kw):
        from src.parser.cabal_parser import CabalSignal
        base = dict(
            pair="RUNE/USDT", side="LONG", entry=0.3895, stop_loss=0.3650,
            stop_is_conditional=True, take_profits=[0.4030, 0.4650],
            leverage=None,
        )
        base.update(kw)
        return CabalSignal(**base)

    @pytest.mark.asyncio
    async def test_propose_dms_and_confirm_places(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, dry_run=False)
        await _opt_in(prefs_db)
        await engine.propose_copy(self._cabal_sig(), source="Cabal Chat")
        assert send_dm.await_count == 1
        preview = send_dm.await_args.args[1]
        assert "RUNE/USDT LONG 5x" in preview          # default leverage
        assert "conditional" in preview                 # SL caveat surfaced
        venue.place.assert_not_called()                 # nothing fired yet

        ok = await engine.confirm_copy(UID)
        assert ok is True
        venue.place.assert_awaited_once()
        kw = venue.place.await_args.kwargs
        assert kw["take_profits"] == [0.4030, 0.4650]
        assert kw["stop_loss"] == 0.3650

    @pytest.mark.asyncio
    async def test_stated_leverage_copied(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, dry_run=False)
        await _opt_in(prefs_db)
        await engine.propose_copy(self._cabal_sig(leverage=20))
        assert "20x" in send_dm.await_args.args[1]

    @pytest.mark.asyncio
    async def test_confirm_without_proposal(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, dry_run=False)
        await _opt_in(prefs_db)
        ok = await engine.confirm_copy(UID)
        assert ok is False
        venue.place.assert_not_called()
        assert "no pending" in send_dm.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_expired_proposal_does_not_fire(self, prefs_db):
        import time as _time
        engine, venue, send_dm = _make_engine(prefs_db, dry_run=False)
        await _opt_in(prefs_db)
        await engine.propose_copy(self._cabal_sig())
        sig, _ = engine._pending_copies[UID]
        engine._pending_copies[UID] = (sig, _time.time() - 1)  # force expiry
        ok = await engine.confirm_copy(UID)
        assert ok is False
        venue.place.assert_not_called()
        assert "expired" in send_dm.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_not_connected_gets_no_proposal(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, dry_run=False, connected=False)
        await _opt_in(prefs_db)
        await engine.propose_copy(self._cabal_sig())
        send_dm.assert_not_called()
        assert UID not in engine._pending_copies


class TestManualFire:
    @pytest.mark.asyncio
    async def test_manual_fire_places_for_invoker(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, dry_run=False)
        await _opt_in(prefs_db)
        await engine.manual_fire(UID, pair="XLM/USDT", side="short", leverage=5, stop_loss=0.207)
        venue.place.assert_awaited_once()
        venue.mark_success.assert_awaited_once()
        assert "filled" in send_dm.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_manual_fire_dry_run_previews(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, dry_run=True)
        await _opt_in(prefs_db)
        await engine.manual_fire(UID, pair="XLM/USDT", side="long", leverage=5)
        venue.place.assert_not_called()
        assert "[DRY RUN]" in send_dm.await_args.args[1]

    @pytest.mark.asyncio
    async def test_manual_fire_bad_direction(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, dry_run=False)
        await _opt_in(prefs_db)
        await engine.manual_fire(UID, pair="XLM/USDT", side="sideways", leverage=5)
        venue.place.assert_not_called()
        assert "direction" in send_dm.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_manual_fire_not_allowlisted(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, allowlist=frozenset({999}), dry_run=False)
        await _opt_in(prefs_db)
        await engine.manual_fire(UID, pair="XLM/USDT", side="short", leverage=5)
        venue.place.assert_not_called()


class TestWalletCopyProposals:
    """Wallet-watcher proposals: size_pct_override + note passthrough."""

    def _wallet_sig(self, **kw):
        base = dict(
            pair="HYPE/USDT", side="LONG", leverage=10, entry=25.0,
            take_profits=[28.0, 31.0, 34.0], stop_loss=22.0,
            size_pct_override=2.0,
            note="SL/TPs are OURS, derived from recent volatility (ATR).",
        )
        base.update(kw)
        return SimpleNamespace(**base)

    @pytest.mark.asyncio
    async def test_override_shrinks_collateral(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, dry_run=False)
        await _opt_in(prefs_db)
        await engine.propose_copy(self._wallet_sig(), source="wallet 0xadd1..5e0d")
        ok = await engine.confirm_copy(UID)
        assert ok is True
        # balance 1000, override 2% < pref 5% -> $20 collateral
        assert venue.plan.await_args.kwargs["collateral_usdc"] == pytest.approx(20.0)

    @pytest.mark.asyncio
    async def test_override_capped_at_user_pref(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, dry_run=False)
        await _opt_in(prefs_db)
        await engine.propose_copy(self._wallet_sig(size_pct_override=50.0))
        await engine.confirm_copy(UID)
        # override 50% capped at the 5% pref -> $50 of the $1000 balance
        assert venue.plan.await_args.kwargs["collateral_usdc"] == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_preview_shows_note_and_capped_pct(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, dry_run=False)
        await _opt_in(prefs_db)
        await engine.propose_copy(self._wallet_sig(), source="wallet 0xadd1..5e0d")
        preview = send_dm.await_args.args[1]
        assert "wallet 0xadd1..5e0d" in preview
        assert "ATR" in preview
        assert "at your 2% size" in preview

    @pytest.mark.asyncio
    async def test_no_override_keeps_pref_sizing(self, prefs_db):
        engine, venue, send_dm = _make_engine(prefs_db, dry_run=False)
        await _opt_in(prefs_db)
        await engine.propose_copy(self._wallet_sig(size_pct_override=None))
        await engine.confirm_copy(UID)
        assert venue.plan.await_args.kwargs["collateral_usdc"] == pytest.approx(50.0)
        assert "at your 5% size" in send_dm.await_args_list[0].args[1]
