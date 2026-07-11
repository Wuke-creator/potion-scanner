"""Tests for the wallet watcher: position diffing, ATR level derivation,
and the poll loop's gates. All IO faked; a real WalletMetricsDB on tmp
path keeps the baseline/restart semantics honest.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.config.settings import WalletCopyConfig
from src.trading.hl_info_client import WalletPosition, WalletState
from src.trading.wallet_metrics_db import (
    StoredPosition,
    TrackedWallet,
    WalletMetricsDB,
)
from src.trading.wallet_watcher import (
    WalletWatcher,
    compute_atr,
    derive_protective_levels,
    diff_positions,
)

ADDR = "0xadd12adbbd5db87674b38af99b6dd34dd2a45e0d"


def _pos(coin="HYPE", szi=100.0, entry=25.0, lev=10.0, notional=2500.0,
         margin=250.0):
    return WalletPosition(
        coin=coin, szi=szi, entry_px=entry, leverage=lev,
        notional=notional, margin_used=margin,
    )


def _stored(coin="HYPE", szi=100.0):
    return StoredPosition(coin=coin, szi=szi, entry_px=25.0)


def _state(*positions, account=10_000.0):
    return WalletState(
        account_value=account,
        positions={p.coin: p for p in positions},
    )


class TestDiffPositions:
    def test_open(self):
        d = diff_positions({}, {"HYPE": _pos()})
        assert [x.kind for x in d] == ["open"]
        assert d[0].curr_szi == 100.0

    def test_add_reduce(self):
        prev = {"HYPE": _stored(szi=100.0)}
        add = diff_positions(prev, {"HYPE": _pos(szi=150.0)})
        red = diff_positions(prev, {"HYPE": _pos(szi=60.0)})
        assert [x.kind for x in add] == ["add"]
        assert [x.kind for x in red] == ["reduce"]

    def test_close(self):
        d = diff_positions({"HYPE": _stored()}, {})
        assert [x.kind for x in d] == ["close"]
        assert d[0].prev_szi == 100.0 and d[0].curr_szi == 0.0

    def test_flip(self):
        d = diff_positions({"HYPE": _stored(szi=100.0)}, {"HYPE": _pos(szi=-50.0)})
        assert [x.kind for x in d] == ["flip"]

    def test_no_change(self):
        assert diff_positions({"HYPE": _stored()}, {"HYPE": _pos()}) == []

    def test_short_add(self):
        d = diff_positions(
            {"ZEC": _stored(coin="ZEC", szi=-10.0)},
            {"ZEC": _pos(coin="ZEC", szi=-20.0)},
        )
        assert [x.kind for x in d] == ["add"]


class TestATR:
    def _candles(self, n=30, base=100.0, rng=2.0):
        out = []
        for i in range(n):
            px = base + (i % 3)
            out.append({
                "t": i, "o": str(px), "h": str(px + rng),
                "l": str(px - rng), "c": str(px),
            })
        return out

    def test_atr_positive(self):
        atr = compute_atr(self._candles(), period=14)
        assert atr is not None and atr >= 4.0   # h-l is 2*rng

    def test_too_few_candles(self):
        assert compute_atr(self._candles(n=10), period=14) is None

    def test_garbage_rows_skipped(self):
        candles = self._candles() + [{"h": "x"}]
        assert compute_atr(candles, period=14) is not None

    def test_levels_long(self):
        stop, tps = derive_protective_levels(
            entry=100.0, is_long=True, atr=2.0, atr_mult=1.5,
        )
        assert stop == pytest.approx(97.0)
        assert tps == [pytest.approx(103.0), pytest.approx(106.0),
                       pytest.approx(109.0)]

    def test_levels_short(self):
        stop, tps = derive_protective_levels(
            entry=100.0, is_long=False, atr=2.0, atr_mult=1.5,
        )
        assert stop == pytest.approx(103.0)
        assert tps[0] == pytest.approx(97.0)

    def test_short_near_zero_drops_negative_tps(self):
        stop, tps = derive_protective_levels(
            entry=1.0, is_long=False, atr=0.3, atr_mult=1.5,
        )
        assert all(t > 0 for t in tps)
        assert len(tps) == 2   # third leg would be <= 0


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    d = WalletMetricsDB(db_path=str(tmp_path / "wallet_scout.db"))
    await d.open()
    await d.save_tracked_wallet(TrackedWallet(address=ADDR, status="tracked"))
    yield d
    await d.close()


def _candles_ok(n=30):
    return [
        {"t": i, "o": "25", "h": "26", "l": "24", "c": "25"} for i in range(n)
    ]


def _make_watcher(db, *, states, listed=True, cfg=None, candles=None):
    """states: list of WalletState returned per successive fetch."""
    info = AsyncMock()
    seq = list(states)

    async def _next_state(addr):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    info.get_clearinghouse_state = AsyncMock(side_effect=_next_state)
    info.get_candles = AsyncMock(
        return_value=candles if candles is not None else _candles_ok(),
    )
    engine = AsyncMock()
    blofin = AsyncMock()
    blofin.resolve_inst_id = AsyncMock(
        return_value=object() if listed else None,
    )
    dms: list[str] = []

    async def _dm(uid, text):
        dms.append(text)

    watcher = WalletWatcher(
        info_client=info, metrics_db=db, engine=engine,
        blofin_client=blofin, config=cfg or WalletCopyConfig(),
        send_dm=_dm, allowlist=frozenset({99}),
    )
    return watcher, engine, dms, info


class TestWatcherPoll:
    @pytest.mark.asyncio
    async def test_first_poll_baselines_without_firing(self, db):
        watcher, engine, dms, _ = _make_watcher(
            db, states=[_state(_pos())],
        )
        await watcher.poll_once()
        engine.propose_copy.assert_not_called()
        assert dms == []
        assert (await db.get_positions(ADDR))["HYPE"].szi == 100.0

    @pytest.mark.asyncio
    async def test_new_open_proposes_with_levels_and_sizing(self, db):
        watcher, engine, dms, _ = _make_watcher(
            db,
            states=[
                _state(account=10_000.0),                      # baseline: flat
                _state(_pos(margin=1000.0), account=10_000.0), # open appears
            ],
        )
        await watcher.poll_once()
        await watcher.poll_once()
        engine.propose_copy.assert_awaited_once()
        sig = engine.propose_copy.await_args.args[0]
        kw = engine.propose_copy.await_args.kwargs
        assert sig.pair == "HYPE/USDT"
        assert sig.side == "LONG"
        assert sig.size_pct_override == pytest.approx(10.0)   # 1000/10000
        assert sig.stop_loss is not None and sig.stop_loss < 25.0
        assert len(sig.take_profits) == 3
        assert "ATR" in sig.note
        assert "wallet 0xadd1..5e0d" == kw["source"]

    @pytest.mark.asyncio
    async def test_short_open_derives_short_levels(self, db):
        watcher, engine, _, _ = _make_watcher(
            db,
            states=[
                _state(account=10_000.0),
                _state(_pos(szi=-100.0, margin=1000.0), account=10_000.0),
            ],
        )
        await watcher.poll_once()
        await watcher.poll_once()
        sig = engine.propose_copy.await_args.args[0]
        assert sig.side == "SHORT"
        assert sig.stop_loss > 25.0
        assert sig.take_profits[0] < 25.0

    @pytest.mark.asyncio
    async def test_conviction_floor_skips_dust(self, db):
        watcher, engine, dms, _ = _make_watcher(
            db,
            states=[
                _state(account=100_000.0),
                _state(_pos(margin=100.0), account=100_000.0),  # 0.1% of equity
            ],
        )
        await watcher.poll_once()
        await watcher.poll_once()
        engine.propose_copy.assert_not_called()

    @pytest.mark.asyncio
    async def test_scalper_wallet_never_proposes(self, db):
        await db.save_tracked_wallet(TrackedWallet(
            address=ADDR, status="tracked", is_scalper=True,
        ))
        watcher, engine, _, _ = _make_watcher(
            db,
            states=[
                _state(account=10_000.0),
                _state(_pos(margin=1000.0), account=10_000.0),
            ],
        )
        await watcher.poll_once()
        await watcher.poll_once()
        engine.propose_copy.assert_not_called()

    @pytest.mark.asyncio
    async def test_unlisted_coin_dms_but_never_proposes(self, db):
        watcher, engine, dms, _ = _make_watcher(
            db, listed=False,
            states=[
                _state(account=10_000.0),
                _state(_pos(margin=1000.0), account=10_000.0),
            ],
        )
        await watcher.poll_once()
        await watcher.poll_once()
        engine.propose_copy.assert_not_called()
        assert any("not" in t.lower() and "listed" in t.lower() for t in dms)

    @pytest.mark.asyncio
    async def test_cooldown_blocks_reopen_spam(self, db):
        watcher, engine, _, _ = _make_watcher(
            db,
            states=[
                _state(account=10_000.0),
                _state(_pos(margin=1000.0), account=10_000.0),   # open
                _state(account=10_000.0),                        # close
                _state(_pos(margin=1000.0), account=10_000.0),   # reopen fast
            ],
        )
        for _ in range(4):
            await watcher.poll_once()
        engine.propose_copy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_dms_immediately_no_auto_reduce(self, db):
        watcher, engine, dms, _ = _make_watcher(
            db,
            states=[
                _state(_pos(margin=1000.0), account=10_000.0),
                _state(account=10_000.0),   # closed
            ],
        )
        await watcher.poll_once()   # baseline includes the open position
        await watcher.poll_once()
        assert any("CLOSED" in t for t in dms)
        engine.propose_copy.assert_not_called()

    @pytest.mark.asyncio
    async def test_small_reduce_is_noise(self, db):
        watcher, engine, dms, _ = _make_watcher(
            db,
            states=[
                _state(_pos(szi=100.0), account=10_000.0),
                _state(_pos(szi=95.0), account=10_000.0),   # -5%: dust
            ],
        )
        await watcher.poll_once()
        await watcher.poll_once()
        assert dms == []

    @pytest.mark.asyncio
    async def test_big_reduce_dms(self, db):
        watcher, engine, dms, _ = _make_watcher(
            db,
            states=[
                _state(_pos(szi=100.0), account=10_000.0),
                _state(_pos(szi=40.0), account=10_000.0),
            ],
        )
        await watcher.poll_once()
        await watcher.poll_once()
        assert any("reduced" in t for t in dms)
        engine.propose_copy.assert_not_called()

    @pytest.mark.asyncio
    async def test_flip_warns_then_proposes_new_side(self, db):
        watcher, engine, dms, _ = _make_watcher(
            db,
            states=[
                _state(_pos(szi=100.0), account=10_000.0),
                _state(_pos(szi=-80.0, margin=1000.0), account=10_000.0),
            ],
        )
        await watcher.poll_once()
        await watcher.poll_once()
        assert any("FLIPPED" in t for t in dms)
        engine.propose_copy.assert_awaited_once()
        assert engine.propose_copy.await_args.args[0].side == "SHORT"

    @pytest.mark.asyncio
    async def test_restart_reports_stale_deltas_without_proposing(self, db):
        # first watcher instance baselines with an open position
        w1, e1, _, _ = _make_watcher(
            db, states=[_state(_pos(), account=10_000.0)],
        )
        await w1.poll_once()
        # "restart": new instance; the wallet closed while we were down
        # and opened something else
        w2, e2, dms2, _ = _make_watcher(
            db,
            states=[_state(_pos(coin="ZEC", szi=-5.0, margin=1000.0),
                           account=10_000.0)],
        )
        await w2.poll_once()
        e2.propose_copy.assert_not_called()
        assert any("offline" in t for t in dms2)
        # next poll continues from the fresh baseline: no deltas, no noise
        await w2.poll_once()
        e2.propose_copy.assert_not_called()

    @pytest.mark.asyncio
    async def test_atr_failure_still_proposes_with_warning(self, db):
        watcher, engine, _, _ = _make_watcher(
            db, candles=[],
            states=[
                _state(account=10_000.0),
                _state(_pos(margin=1000.0), account=10_000.0),
            ],
        )
        await watcher.poll_once()
        await watcher.poll_once()
        sig = engine.propose_copy.await_args.args[0]
        assert sig.stop_loss is None
        assert "NO stop" in sig.note

    @pytest.mark.asyncio
    async def test_k_coin_maps_to_1000_base(self, db):
        watcher, engine, _, info = _make_watcher(
            db,
            states=[
                _state(account=10_000.0),
                _state(_pos(coin="kPEPE", margin=1000.0), account=10_000.0),
            ],
        )
        await watcher.poll_once()
        await watcher.poll_once()
        sig = engine.propose_copy.await_args.args[0]
        assert sig.pair == "1000PEPE/USDT"
