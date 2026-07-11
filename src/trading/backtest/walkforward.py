"""Walk-forward validation of the SCOUT PIPELINE (not of any wallet).

Backtesting today's winners on the history that made them winners is
selection on the dependent variable. This module answers the only
defensible question: had the scout run each week over the universe AS IT
LOOKED THAT WEEK (archived leaderboard snapshots), and had we copied its
tracked set the FOLLOWING week under our live copy rules, what would the
copier have earned, and does the copyability score at selection time
predict next-week copy PnL at all (Spearman rank correlation; the
academic prior for social trading is approximately zero).

Requires the snapshot archive: weeks before the first archived snapshot
simply cannot be validated point-in-time and are never synthesized here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.config.settings import BacktestConfig, WalletCopyConfig
from src.trading.backtest.data_feed import BacktestDataFeed, _date_to_ms
from src.trading.backtest.data_store import BacktestStore
from src.trading.backtest.position_events import reconstruct_position_events
from src.trading.backtest.runner import Progress, _base_params
from src.trading.backtest.simulator import simulate_wallet
from src.trading.hl_info_client import parse_leaderboard_row
from src.trading.wallet_metrics_db import TrackedWallet
from src.trading.wallet_scout import (
    apply_hysteresis,
    score_wallet_asof,
    screen_leaderboard,
    short_addr,
)

logger = logging.getLogger(__name__)

WEEK_MS = 7 * 86_400_000
MIN_SNAPSHOT_WEEKS = 3


@dataclass
class WalkWeek:
    select_date: str
    tracked: list[str] = field(default_factory=list)
    promoted: list[str] = field(default_factory=list)
    demoted: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    next_week_net: dict[str, float] = field(default_factory=dict)
    net_usd: float = 0.0
    n_trades: int = 0


@dataclass
class WalkForwardResult:
    weeks: list[WalkWeek] = field(default_factory=list)
    total_net_usd: float = 0.0
    total_trades: int = 0
    spearman: float | None = None
    n_score_pnl_pairs: int = 0
    insufficient_archive: str = ""


def spearman_rank(pairs: list[tuple[float, float]]) -> float | None:
    """Spearman rho with average ranks for ties. None below 5 pairs."""
    n = len(pairs)
    if n < 5:
        return None

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: values[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    xs = ranks([p[0] for p in pairs])
    ys = ranks([p[1] for p in pairs])
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx * vy) ** 0.5


class WalkForward:
    def __init__(
        self,
        *,
        feed: BacktestDataFeed,
        store: BacktestStore,
        wallet_cfg: WalletCopyConfig,
        backtest_cfg: BacktestConfig,
    ):
        self._feed = feed
        self._store = store
        self._wallet_cfg = wallet_cfg
        self._cfg = backtest_cfg

    async def run(self, progress: Progress) -> WalkForwardResult:
        result = WalkForwardResult()
        dates = await self._store.list_leaderboard_snapshot_dates()
        weekly = dates[::7] if len(dates) >= 7 else dates[:: max(1, len(dates))]
        # only weeks whose FOLLOWING week is fully in the past can be scored
        now_ms = int(
            datetime.now(timezone.utc).timestamp() * 1000,
        )
        weekly = [d for d in weekly if _date_to_ms(d) + WEEK_MS <= now_ms]
        if len(weekly) < MIN_SNAPSHOT_WEEKS:
            result.insufficient_archive = (
                f"only {len(weekly)} scoreable weekly snapshot(s) archived; "
                f"need {MIN_SNAPSHOT_WEEKS}. The archive started with the "
                "snapshot job; give it time."
            )
            return result

        state: dict[str, TrackedWallet] = {}
        score_pnl_pairs: list[tuple[float, float]] = []

        for wi, date in enumerate(weekly, 1):
            await progress(f"walk-forward week {wi}/{len(weekly)}: {date}")
            week = await self._run_week(date, state)
            state = week["state"]
            wk: WalkWeek = week["week"]
            result.weeks.append(wk)
            result.total_net_usd += wk.net_usd
            result.total_trades += wk.n_trades
            for addr, score in wk.scores.items():
                if addr in wk.next_week_net:
                    score_pnl_pairs.append((score, wk.next_week_net[addr]))

        result.n_score_pnl_pairs = len(score_pnl_pairs)
        result.spearman = spearman_rank(score_pnl_pairs)
        return result

    async def _run_week(
        self, date: str, state: dict[str, TrackedWallet],
    ) -> dict:
        t_ms = _date_to_ms(date)
        board = await self._store.load_leaderboard_snapshot(date)
        raw_rows = (
            board.get("leaderboardRows") if isinstance(board, dict) else board
        ) or []
        parsed = []
        for rr in raw_rows:
            if isinstance(rr, dict):
                row = parse_leaderboard_row(rr)
                if row is not None:
                    parsed.append(row)
        screened = screen_leaderboard(parsed, self._wallet_cfg)
        by_addr = {r.address.lower(): r for r in parsed}

        finalists = [r.address for r in screened[: self._wallet_cfg.max_finalists]]
        for addr in state:
            if addr not in finalists:
                finalists.append(addr)

        wk = WalkWeek(select_date=date)
        today: dict[str, tuple[float, bool]] = {}
        for addr in finalists:
            row = by_addr.get(addr.lower())
            if row is None:
                continue
            fills, _ = await self._feed.fills(
                addr, start_ms=t_ms - 60 * 86_400_000, end_ms=t_ms,
            )
            score, stats = score_wallet_asof(
                row, fills, as_of_ms=t_ms, blofin_coverage=1.0,
                cfg=self._wallet_cfg,
            )
            is_scalper = (
                stats.fills_per_day > self._wallet_cfg.scalper_fills_per_day
            )
            today[addr] = (score, is_scalper)
            wk.scores[addr] = score

        hyst = apply_hysteresis(state, today, self._wallet_cfg)
        wk.promoted, wk.demoted = hyst.promoted, hyst.demoted
        new_state = hyst.wallets
        tracked = [
            w.address for w in new_state.values() if w.status == "tracked"
        ]
        wk.tracked = [short_addr(a) for a in tracked]

        # evaluate the FOLLOWING week only: out-of-sample by construction
        params = _base_params(self._cfg)
        for addr in tracked:
            fills, _ = await self._feed.fills(
                addr, start_ms=t_ms, end_ms=t_ms + WEEK_MS,
            )
            if not fills:
                wk.next_week_net.setdefault(addr, 0.0)
                continue
            curve = await self._feed.account_value_curve(addr, fills)
            events = reconstruct_position_events(
                fills, account_value_at=curve.at,
            )
            coins = {e.coin for e in events}
            if not coins:
                wk.next_week_net.setdefault(addr, 0.0)
                continue
            market = await self._feed.market_data(
                coins, start_ms=t_ms, end_ms=t_ms + WEEK_MS,
            )
            trades, _skips = simulate_wallet(addr, events, market, params)
            net = sum(t.net_pnl_usd for t in trades)
            wk.next_week_net[addr] = net
            wk.net_usd += net
            wk.n_trades += len(trades)

        return {"state": new_state, "week": wk}


def format_walkforward(result: WalkForwardResult) -> str:
    if result.insufficient_archive:
        return (
            "Walk-forward validation: NOT POSSIBLE YET.\n"
            + result.insufficient_archive
            + "\nEverything backtested before the archive matured is a "
            "survivorship-contaminated upper bound, not validation."
        )
    lines = [
        f"Walk-forward: {len(result.weeks)} out-of-sample week(s), "
        f"{result.total_trades} trades, net ${result.total_net_usd:,.2f} "
        "(per $1k notional/trade).",
    ]
    if result.spearman is not None:
        lines.append(
            f"Score->next-week-PnL Spearman: {result.spearman:+.2f} over "
            f"{result.n_score_pnl_pairs} pairs. (Academic prior ~0; a value "
            "near 0 means rank-chasing adds nothing and the score should "
            "lean harder on risk filters.)"
        )
    else:
        lines.append(
            f"Spearman: n/a ({result.n_score_pnl_pairs} pairs, need 5+).",
        )
    for wk in result.weeks[-8:]:
        change = ""
        if wk.promoted or wk.demoted:
            change = f" (+{len(wk.promoted)}/-{len(wk.demoted)})"
        lines.append(
            f"  {wk.select_date}: tracked {len(wk.tracked)}{change}, "
            f"next-week net ${wk.net_usd:,.2f} on {wk.n_trades} trade(s)",
        )
    return "\n".join(lines)
