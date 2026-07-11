"""Statistics over simulated copy trades. Pure, deterministic (seeded).

Sample-discipline rules baked in rather than left to the reader:
  - the bootstrap CI is CLUSTERED (by UTC day, coin as sensitivity):
    cross-wallet trades on the same day/coin are correlated and an iid
    bootstrap would fake precision;
  - the CI is suppressed (None) below MIN_CLUSTERS clusters;
  - the atr_mult plateau is a TABLE for the report; nothing in here
    selects a best cell.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.trading.backtest.simulator import SimTrade

MIN_CLUSTERS = 10
N_BOOT = 2000


def _day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d",
    )


@dataclass
class TradeSummary:
    n: int = 0
    wins: int = 0
    win_rate: float = 0.0
    gross_usd: float = 0.0
    fees_usd: float = 0.0
    funding_usd: float = 0.0
    net_usd: float = 0.0
    expectancy_usd: float = 0.0        # net per trade, per $1k notional
    profit_factor: float = 0.0
    avg_r: float | None = None
    median_r: float | None = None
    r_values: list[float] = field(default_factory=list)
    n_mtm: int = 0                     # unresolved, marked to market
    ci_low: float | None = None        # clustered bootstrap CI on expectancy
    ci_high: float | None = None
    ci_suppressed_reason: str = ""


def summarize(
    trades: list[SimTrade], *, seed: int = 7, cluster: str = "day",
) -> TradeSummary:
    s = TradeSummary()
    s.n = len(trades)
    if not trades:
        s.ci_suppressed_reason = "no trades"
        return s
    nets = [t.net_pnl_usd for t in trades]
    s.wins = sum(1 for x in nets if x > 0)
    s.win_rate = s.wins / s.n
    s.gross_usd = sum(t.gross_pnl_usd for t in trades)
    s.fees_usd = sum(t.fees_usd for t in trades)
    s.funding_usd = sum(t.funding_usd for t in trades)
    s.net_usd = sum(nets)
    s.expectancy_usd = s.net_usd / s.n
    gross_win = sum(x for x in nets if x > 0)
    gross_loss = abs(sum(x for x in nets if x < 0))
    s.profit_factor = (
        gross_win / gross_loss if gross_loss > 0
        else (float("inf") if gross_win > 0 else 0.0)
    )
    s.r_values = [t.r_multiple for t in trades if t.r_multiple is not None]
    if s.r_values:
        rs = sorted(s.r_values)
        s.avg_r = sum(rs) / len(rs)
        mid = len(rs) // 2
        s.median_r = (
            rs[mid] if len(rs) % 2 == 1 else (rs[mid - 1] + rs[mid]) / 2
        )
    s.n_mtm = sum(1 for t in trades if t.resolution == "mtm")
    low, high, why = bootstrap_ci(trades, seed=seed, cluster=cluster)
    s.ci_low, s.ci_high, s.ci_suppressed_reason = low, high, why
    return s


def bootstrap_ci(
    trades: list[SimTrade], *, seed: int = 7, cluster: str = "day",
    n_boot: int = N_BOOT, alpha: float = 0.05,
) -> tuple[float | None, float | None, str]:
    """Clustered bootstrap CI on mean net pnl per trade."""
    if not trades:
        return None, None, "no trades"
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        key = _day(t.entry_ts) if cluster == "day" else t.coin
        groups[key].append(t.net_pnl_usd)
    clusters = list(groups.values())
    if len(clusters) < MIN_CLUSTERS:
        return None, None, (
            f"only {len(clusters)} {cluster}-clusters (<{MIN_CLUSTERS}); "
            "CI not defensible"
        )
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_boot):
        sample: list[float] = []
        for _ in range(len(clusters)):
            sample.extend(clusters[rng.randrange(len(clusters))])
        if sample:
            means.append(sum(sample) / len(sample))
    means.sort()
    lo_i = int((alpha / 2) * len(means))
    hi_i = min(len(means) - 1, int((1 - alpha / 2) * len(means)))
    return means[lo_i], means[hi_i], ""


def latency_robustness(
    net_by_delay_sec: dict[int, float],
) -> float | None:
    """PnL at the slowest human delay over PnL at zero delay.

    > ~0.7 means the edge survives our confirm latency. None when the
    zero-delay PnL is not positive (there is no edge to be robust about,
    or the ratio would be meaningless).
    """
    if not net_by_delay_sec:
        return None
    base = net_by_delay_sec.get(0)
    slow = net_by_delay_sec.get(max(net_by_delay_sec))
    if base is None or slow is None or base <= 0:
        return None
    return slow / base


@dataclass
class PairedComparison:
    n_pairs: int = 0
    mean_diff_usd: float = 0.0         # engine A minus engine B, per trade
    wins_a: int = 0                    # pairs where A beat B
    ci_low: float | None = None
    ci_high: float | None = None
    ci_suppressed_reason: str = ""


def paired_exit_comparison(
    trades_a: list[SimTrade], trades_b: list[SimTrade], *, seed: int = 7,
) -> PairedComparison:
    """Same entries, two exit engines: per-pair net difference.

    Pairs on (leader, coin, event_ts). Unpaired trades are dropped, so a
    gate difference can never masquerade as an exit-policy difference.
    """
    res = PairedComparison()
    index_b = {(t.leader, t.coin, t.event_ts): t for t in trades_b}
    diffs: list[tuple[int, float]] = []
    for ta in trades_a:
        tb = index_b.get((ta.leader, ta.coin, ta.event_ts))
        if tb is None:
            continue
        diffs.append((ta.entry_ts, ta.net_pnl_usd - tb.net_pnl_usd))
    res.n_pairs = len(diffs)
    if not diffs:
        res.ci_suppressed_reason = "no pairs"
        return res
    values = [d for _, d in diffs]
    res.mean_diff_usd = sum(values) / len(values)
    res.wins_a = sum(1 for d in values if d > 0)
    groups: dict[str, list[float]] = defaultdict(list)
    for ts, d in diffs:
        groups[_day(ts)].append(d)
    clusters = list(groups.values())
    if len(clusters) < MIN_CLUSTERS:
        res.ci_suppressed_reason = (
            f"only {len(clusters)} day-clusters (<{MIN_CLUSTERS})"
        )
        return res
    rng = random.Random(seed)
    means = []
    for _ in range(N_BOOT):
        sample: list[float] = []
        for _ in range(len(clusters)):
            sample.extend(clusters[rng.randrange(len(clusters))])
        if sample:
            means.append(sum(sample) / len(sample))
    means.sort()
    res.ci_low = means[int(0.025 * len(means))]
    res.ci_high = means[min(len(means) - 1, int(0.975 * len(means)))]
    return res


def plateau_table(
    summaries_by_atr_mult: dict[float, TradeSummary],
) -> list[tuple[float, float, float | None]]:
    """(atr_mult, expectancy, avg_r) rows for the report. A robust edge
    shows a plateau across mults; a spike at one mult is noise. This is
    a display table: nothing selects the best row."""
    return [
        (mult, s.expectancy_usd, s.avg_r)
        for mult, s in sorted(summaries_by_atr_mult.items())
    ]
