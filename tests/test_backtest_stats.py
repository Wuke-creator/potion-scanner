"""Tests for backtest statistics: summaries, clustered bootstrap,
latency robustness, paired exit comparison. Seeded and deterministic."""

from __future__ import annotations

import pytest

from src.trading.backtest.simulator import SimTrade
from src.trading.backtest.stats import (
    MIN_CLUSTERS,
    PairedComparison,
    bootstrap_ci,
    latency_robustness,
    paired_exit_comparison,
    plateau_table,
    summarize,
)

T0 = 1_760_000_000_000
DAY = 86_400_000


def _trade(net, *, day=0, coin="A", r=None, event_ts=None, leader="0xL",
           resolution="1m"):
    ts = T0 + day * DAY
    return SimTrade(
        leader=leader, coin=coin, side="LONG",
        event_ts=event_ts if event_ts is not None else ts,
        entry_ts=ts, entry_px=100.0, stop_px=94.0, tp_prices=[],
        gross_pnl_usd=net, fees_usd=0.0, funding_usd=0.0, net_pnl_usd=net,
        r_multiple=r, resolution=resolution,
    )


class TestSummarize:
    def test_empty(self):
        s = summarize([])
        assert s.n == 0 and s.ci_low is None

    def test_basic_stats(self):
        trades = [
            _trade(50.0, day=i, r=1.0) for i in range(3)
        ] + [_trade(-25.0, day=3, r=-0.5)]
        s = summarize(trades)
        assert s.n == 4 and s.wins == 3
        assert s.win_rate == pytest.approx(0.75)
        assert s.net_usd == pytest.approx(125.0)
        assert s.expectancy_usd == pytest.approx(31.25)
        assert s.profit_factor == pytest.approx(150.0 / 25.0)
        assert s.avg_r == pytest.approx((3 - 0.5) / 4)
        assert s.median_r == pytest.approx(1.0)

    def test_mtm_counted(self):
        s = summarize([_trade(1.0, resolution="mtm"), _trade(1.0)])
        assert s.n_mtm == 1

    def test_ci_suppressed_below_min_clusters(self):
        trades = [_trade(10.0, day=i % 3) for i in range(30)]
        s = summarize(trades)
        assert s.ci_low is None
        assert "3 day-clusters" in s.ci_suppressed_reason

    def test_ci_present_with_enough_clusters(self):
        trades = [
            _trade(10.0 if i % 2 else -5.0, day=i) for i in range(MIN_CLUSTERS + 5)
        ]
        s = summarize(trades)
        assert s.ci_low is not None and s.ci_high is not None
        assert s.ci_low <= s.expectancy_usd <= s.ci_high

    def test_bootstrap_deterministic_with_seed(self):
        trades = [_trade(float(i - 6), day=i) for i in range(14)]
        a = bootstrap_ci(trades, seed=42)
        b = bootstrap_ci(trades, seed=42)
        c = bootstrap_ci(trades, seed=43)
        assert a == b
        assert a != c

    def test_coin_cluster_mode(self):
        trades = [_trade(5.0, day=0, coin=f"C{i}") for i in range(12)]
        lo, hi, why = bootstrap_ci(trades, cluster="coin")
        assert why == "" and lo is not None


class TestLatencyRobustness:
    def test_ratio(self):
        assert latency_robustness({0: 100.0, 900: 75.0}) == pytest.approx(0.75)

    def test_no_edge_at_zero_delay_is_none(self):
        assert latency_robustness({0: -10.0, 900: 5.0}) is None
        assert latency_robustness({}) is None
        assert latency_robustness({900: 5.0}) is None


class TestPairedComparison:
    def test_pairs_on_identical_entries_only(self):
        a = [_trade(10.0, day=i, event_ts=T0 + i) for i in range(4)]
        b = [_trade(4.0, day=i, event_ts=T0 + i) for i in range(3)]
        b.append(_trade(99.0, day=9, event_ts=T0 + 999))  # unpaired: dropped
        res = paired_exit_comparison(a, b)
        assert res.n_pairs == 3
        assert res.mean_diff_usd == pytest.approx(6.0)
        assert res.wins_a == 3

    def test_empty(self):
        res = paired_exit_comparison([], [])
        assert isinstance(res, PairedComparison)
        assert res.n_pairs == 0

    def test_ci_needs_clusters(self):
        a = [_trade(10.0, day=i, event_ts=T0 + i) for i in range(MIN_CLUSTERS + 2)]
        b = [_trade(5.0, day=i, event_ts=T0 + i) for i in range(MIN_CLUSTERS + 2)]
        res = paired_exit_comparison(a, b)
        assert res.ci_low is not None
        assert res.ci_low <= res.mean_diff_usd <= res.ci_high


class TestPlateau:
    def test_table_sorted_and_never_selects(self):
        table = plateau_table({
            2.0: summarize([_trade(5.0)]),
            1.0: summarize([_trade(20.0)]),
            1.5: summarize([_trade(10.0)]),
        })
        assert [row[0] for row in table] == [1.0, 1.5, 2.0]
        assert table[0][1] == pytest.approx(20.0)
