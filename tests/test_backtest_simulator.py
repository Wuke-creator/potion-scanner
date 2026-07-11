"""Tests for the pure copy-trade simulator: every exit rule exercised on
hand-built candles."""

from __future__ import annotations

import pytest

from src.trading.backtest.data_store import Candle
from src.trading.backtest.position_events import LeaderEvent
from src.trading.backtest.simulator import (
    NOTIONAL_USD,
    MarketData,
    SimParams,
    _OpenState,
    atr_at,
    funding_cost_usd,
    resolve_candle,
    simulate_wallet,
)

T0 = 1_760_000_000_000
MIN = 60_000
HOUR = 3_600_000
LEADER = "0xleader"


def _c(ts, o, h, l, c):  # noqa: E741
    return Candle(ts=ts, o=o, h=h, l=l, c=c)


def _flat_1m(t0, n, px=100.0):
    return [_c(t0 + i * MIN, px, px, px, px) for i in range(n)]


def _hourly(t0, n, px=100.0, rng=2.0):
    """1h candles with a constant true range of ~2*rng => ATR ~ 2*rng."""
    return [
        _c(t0 + i * HOUR, px, px + rng, px - rng, px) for i in range(n)
    ]


def _open_event(coin="A", ts=T0, szi=10.0, px=100.0, conviction=0.5):
    return LeaderEvent(
        ts_ms=ts, kind="open", coin=coin, prev_szi=0.0, curr_szi=szi,
        fill_vwap=px, notional=abs(szi) * px, conviction=conviction,
        reduce_fraction=0.0,
    )


def _close_event(coin="A", ts=T0, prev=10.0):
    return LeaderEvent(
        ts_ms=ts, kind="close", coin=coin, prev_szi=prev, curr_szi=0.0,
        fill_vwap=0.0, notional=0.0, conviction=0.0, reduce_fraction=1.0,
    )


def _market(candles_1m, *, t_hist=T0 - 40 * HOUR, px=100.0, rng=2.0,
            funding=None, coin="A"):
    return MarketData(
        candles_1m={coin: candles_1m},
        candles_1h={coin: _hourly(t_hist, 40, px=px, rng=rng)},
        funding={coin: funding or []},
    )


# ATR from _hourly: TR = 2*rng = 4.0 with rng=2 -> risk = 1.5*4 = 6.0
PARAMS = SimParams(confirm_delay_sec=0, slippage_bps=0.0, taker_fee_bps=0.0)


class TestATRHelper:
    def test_uses_only_closed_candles(self):
        candles = _hourly(T0 - 20 * HOUR, 20)
        # candle at T0-1h closes exactly at T0: included. Later ones not.
        atr = atr_at(candles, T0, 14)
        assert atr == pytest.approx(4.0)

    def test_insufficient_history_none(self):
        assert atr_at(_hourly(T0 - 5 * HOUR, 5), T0, 14) is None


class TestResolveCandle:
    def _state(self, *, long=True, entry=100.0, stop=94.0,
               legs=((106.0, 0.5), (112.0, 0.3), (118.0, 0.2))):
        return _OpenState(
            is_long=long, entry_px=entry, stop_px=stop, tp_legs=list(legs),
        )

    def test_no_touch_no_exec(self):
        st = self._state()
        assert resolve_candle(st, _c(T0, 100, 101, 99, 100)) == []
        assert st.remaining == 1.0

    def test_tp1_partial_fills(self):
        st = self._state()
        execs = resolve_candle(st, _c(T0, 100, 107, 99, 106))
        assert len(execs) == 1
        assert execs[0].px == 106.0 and execs[0].fraction == 0.5
        assert st.remaining == pytest.approx(0.5)

    def test_sl_first_when_both_in_candle(self):
        st = self._state()
        execs = resolve_candle(st, _c(T0, 100, 107, 93, 95))
        assert len(execs) == 1
        assert execs[0].reason == "sl" and execs[0].px == 94.0
        assert execs[0].fraction == 1.0
        assert st.remaining == 0.0

    def test_gap_through_stop_fills_at_open(self):
        st = self._state()
        execs = resolve_candle(st, _c(T0, 90, 92, 88, 91))
        assert execs[0].reason == "sl"
        assert execs[0].px == 90.0    # the open, not the stop level

    def test_gap_through_tp_fills_at_open(self):
        st = self._state()
        execs = resolve_candle(st, _c(T0, 108, 109, 107.5, 108))
        assert execs[0].px == 108.0 and execs[0].fraction == 0.5
        assert st.remaining == pytest.approx(0.5)

    def test_short_side_mirrored(self):
        st = self._state(long=False, entry=100.0, stop=106.0,
                         legs=((94.0, 1.0),))
        execs = resolve_candle(st, _c(T0, 100, 101, 93, 94))
        assert execs[0].px == 94.0 and st.remaining == 0.0

    def test_sequential_tps_across_candles(self):
        st = self._state()
        resolve_candle(st, _c(T0, 100, 106.5, 99, 106))
        execs = resolve_candle(st, _c(T0 + MIN, 106, 112.5, 105, 112))
        assert st.remaining == pytest.approx(0.2)
        assert len(execs) == 1 and execs[0].fraction == pytest.approx(0.3)


class TestSimulateWallet:
    def test_win_path_full_ladder(self):
        # price walks up through all three TPs, no stop touch
        candles = (
            _flat_1m(T0, 5)
            + [_c(T0 + 5 * MIN, 100, 107, 100, 106.5)]
            + [_c(T0 + 6 * MIN, 106.5, 113, 106, 112.5)]
            + [_c(T0 + 7 * MIN, 112.5, 119, 112, 118.5)]
        )
        trades, skips = simulate_wallet(
            LEADER, [_open_event()], _market(candles), PARAMS,
        )
        assert not skips
        t = trades[0]
        assert t.side == "LONG" and t.entry_px == 100.0
        assert t.stop_px == pytest.approx(94.0)
        assert t.tp_prices == [pytest.approx(106.0), pytest.approx(112.0),
                               pytest.approx(118.0)]
        assert [e.reason for e in t.exits].count("tp") == 3
        # 0.5*6 + 0.3*12 + 0.2*18 = 10.2 per unit on 10 units ($1000/100)
        assert t.gross_pnl_usd == pytest.approx(102.0)
        assert t.r_multiple == pytest.approx(102.0 / 60.0)

    def test_loss_path_stop(self):
        candles = _flat_1m(T0, 3) + [_c(T0 + 3 * MIN, 99, 99, 92, 93)]
        trades, _ = simulate_wallet(
            LEADER, [_open_event()], _market(candles), PARAMS,
        )
        t = trades[0]
        assert [e.reason for e in t.exits] == ["sl"]
        assert t.gross_pnl_usd == pytest.approx(-60.0)
        assert t.r_multiple == pytest.approx(-1.0)

    def test_fees_charged_per_partial(self):
        params = SimParams(confirm_delay_sec=0, slippage_bps=0.0,
                           taker_fee_bps=10.0)
        candles = (
            _flat_1m(T0, 2)
            + [_c(T0 + 2 * MIN, 100, 107, 100, 106.5)]
            + [_c(T0 + 3 * MIN, 106.5, 119, 106, 118.5)]
        )
        trades, _ = simulate_wallet(
            LEADER, [_open_event()], _market(candles), params,
        )
        t = trades[0]
        # entry fee on $1000 + one fee per exit leg
        assert t.fees_usd > NOTIONAL_USD * 0.001
        assert len([e for e in t.exits if e.fee_usd > 0]) == len(t.exits)

    def test_confirm_delay_shifts_entry(self):
        params = SimParams(confirm_delay_sec=300, slippage_bps=0.0,
                           taker_fee_bps=0.0)
        # price runs up during the delay: later entry, worse basis
        candles = [
            _c(T0 + i * MIN, 100 + i, 100 + i, 100 + i, 100 + i)
            for i in range(30)
        ]
        trades, _ = simulate_wallet(
            LEADER, [_open_event()], _market(candles), params,
        )
        assert trades[0].entry_ts == T0 + 5 * MIN
        assert trades[0].entry_px == pytest.approx(105.0)

    def test_fill_models_ordering(self):
        candles = [_c(T0, 101, 103, 100.5, 102)] + _flat_1m(T0 + MIN, 30, 102)
        nets = {}
        for model in ("optimistic", "realistic", "pessimistic"):
            params = SimParams(confirm_delay_sec=0, fill_model=model,
                               slippage_bps=10.0, taker_fee_bps=0.0,
                               exit_engine="mirror", max_hold_days=0.001)
            trades, _ = simulate_wallet(
                LEADER, [_open_event(px=100.0)], _market(candles), params,
            )
            nets[model] = trades[0].entry_px
        assert nets["optimistic"] <= nets["realistic"] <= nets["pessimistic"]

    def test_conviction_floor_skips(self):
        trades, skips = simulate_wallet(
            LEADER, [_open_event(conviction=0.01)],
            _market(_flat_1m(T0, 10)), PARAMS,
        )
        assert not trades
        assert skips[0].reason == "conviction_floor"

    def test_cooldown_skips_reopen(self):
        events = [
            _open_event(ts=T0),
            _close_event(ts=T0 + 2 * MIN),
            _open_event(ts=T0 + 5 * MIN),   # 5 min later: inside 30m cooldown
        ]
        candles = _flat_1m(T0, 120)
        trades, skips = simulate_wallet(LEADER, events, _market(candles), PARAMS)
        assert len(trades) == 1
        assert any(s.reason == "cooldown" for s in skips)

    def test_leader_close_forces_exit_in_ladder_engine(self):
        events = [_open_event(ts=T0), _close_event(ts=T0 + 10 * MIN)]
        candles = _flat_1m(T0, 60, px=100.0)
        trades, _ = simulate_wallet(LEADER, events, _market(candles), PARAMS)
        t = trades[0]
        assert [e.reason for e in t.exits] == ["leader_exit"]
        assert t.exits[0].ts_ms == T0 + 10 * MIN

    def test_mirror_engine_proportional_reduce(self):
        reduce_ev = LeaderEvent(
            ts_ms=T0 + 10 * MIN, kind="reduce", coin="A", prev_szi=10.0,
            curr_szi=4.0, fill_vwap=100.0, notional=400.0, conviction=0.04,
            reduce_fraction=0.6,
        )
        events = [_open_event(ts=T0), reduce_ev, _close_event(ts=T0 + 20 * MIN)]
        params = SimParams(confirm_delay_sec=0, exit_engine="mirror",
                           slippage_bps=0.0, taker_fee_bps=0.0)
        candles = _flat_1m(T0, 60)
        trades, _ = simulate_wallet(LEADER, events, _market(candles), params)
        t = trades[0]
        assert [e.reason for e in t.exits] == ["leader_exit", "leader_exit"]
        assert t.exits[0].fraction == pytest.approx(0.6)
        assert t.exits[1].fraction == pytest.approx(0.4)

    def test_hop_engine_quick_tp(self):
        params = SimParams(confirm_delay_sec=0, exit_engine="hop",
                           slippage_bps=0.0, taker_fee_bps=0.0,
                           hop_tp_r=0.5, hop_time_stop_min=30.0)
        # +0.5R = +3.0 hit at the 4th minute
        candles = _flat_1m(T0, 3) + [_c(T0 + 3 * MIN, 100, 103.5, 100, 103)]
        trades, _ = simulate_wallet(
            LEADER, [_open_event()], _market(candles), params,
        )
        t = trades[0]
        assert len(t.exits) == 1 and t.exits[0].px == pytest.approx(103.0)
        assert t.gross_pnl_usd == pytest.approx(30.0)

    def test_hop_engine_time_stop(self):
        params = SimParams(confirm_delay_sec=0, exit_engine="hop",
                           slippage_bps=0.0, taker_fee_bps=0.0,
                           hop_tp_r=0.5, hop_time_stop_min=10.0)
        candles = _flat_1m(T0, 60, px=100.0)
        trades, _ = simulate_wallet(
            LEADER, [_open_event()], _market(candles), params,
        )
        t = trades[0]
        assert [e.reason for e in t.exits] == ["time_stop"]
        assert t.exits[0].ts_ms == T0 + 10 * MIN

    def test_no_atr_skips_ladder_but_not_mirror(self):
        market = MarketData(
            candles_1m={"A": _flat_1m(T0, 30)},
            candles_1h={"A": []}, funding={"A": []},
        )
        trades, skips = simulate_wallet(LEADER, [_open_event()], market, PARAMS)
        assert not trades and skips[0].reason == "no_atr_entry"
        params = SimParams(confirm_delay_sec=0, exit_engine="mirror",
                           max_hold_days=0.001)
        trades, _ = simulate_wallet(LEADER, [_open_event()], market, params)
        assert len(trades) == 1
        assert trades[0].r_multiple is None    # no risk unit without ATR

    def test_runs_out_of_candles_marks_mtm(self):
        candles = _flat_1m(T0, 5, px=100.0)
        trades, _ = simulate_wallet(
            LEADER, [_open_event()], _market(candles), PARAMS,
        )
        t = trades[0]
        assert t.resolution == "mtm"
        assert t.exits[-1].reason == "mtm"

    def test_funding_cost_reduces_net(self):
        funding = [(T0 + i * HOUR, 0.0001) for i in range(1, 5)]
        candles = _flat_1m(T0, 6 * 60, px=100.0)
        trades, _ = simulate_wallet(
            LEADER, [_open_event()],
            _market(candles, funding=funding), PARAMS,
        )
        t = trades[0]
        assert t.funding_usd > 0
        assert t.net_pnl_usd < t.gross_pnl_usd

    def test_short_funding_is_received(self):
        assert funding_cost_usd(
            [(T0 + HOUR, 0.0001)], is_long=False,
            notional_open=[(T0, T0 + 2 * HOUR, 1000.0)],
        ) < 0

    def test_deterministic(self):
        candles = _flat_1m(T0, 30) + [_c(T0 + 30 * MIN, 100, 107, 93, 95)]
        a = simulate_wallet(LEADER, [_open_event()], _market(candles), PARAMS)
        b = simulate_wallet(LEADER, [_open_event()], _market(candles), PARAMS)
        assert a == b
