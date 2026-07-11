"""Backtest orchestration: feed -> events -> simulation grid -> stats.

The grid is FIXED (delays, fill bounds, exit engines, a coarse atr_mult
plateau, a conviction-floor sensitivity row). Nothing selects a best
cell; every cell lands in the run log so later analysis can correct for
the number of trials looked at.

CPU-bound simulation cells run in a worker thread so a multi-minute run
never blocks the bot's event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable

from src.config.settings import BacktestConfig, WalletCopyConfig
from src.trading.backtest.data_feed import BacktestDataFeed
from src.trading.backtest.data_store import BacktestStore
from src.trading.backtest.position_events import (
    LeaderEvent,
    reconstruct_position_events,
)
from src.trading.backtest.simulator import (
    MarketData,
    SimParams,
    SimTrade,
    simulate_wallet,
)
from src.trading.backtest.stats import (
    PairedComparison,
    TradeSummary,
    latency_robustness,
    paired_exit_comparison,
    plateau_table,
    summarize,
)
from src.trading.wallet_scout import (
    compute_fill_stats,
    hl_coin_to_blofin_base,
    short_addr,
)

logger = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]

PRIMARY_DELAY_SEC = 120
BOT_SPEED_DELAY_SEC = 15
PLATEAU_MULTS = (1.0, 1.5, 2.0)
FLOOR_SENSITIVITY = (0.15, 0.25, 0.40)


@dataclass
class BacktestSpec:
    addresses: list[str]
    days: int = 60
    label: str = ""


@dataclass
class WalletRunResult:
    address: str
    n_fills: int = 0
    fills_complete: bool = True
    n_events: int = 0
    n_open_events: int = 0
    is_scalper: bool = False
    fills_per_day: float = 0.0
    unmapped_coins: list[str] = field(default_factory=list)
    error: str = ""
    # params_id -> summary for every cell run on this wallet
    summaries: dict[str, TradeSummary] = field(default_factory=dict)
    primary_trades: list[SimTrade] = field(default_factory=list)
    mirror_trades: list[SimTrade] = field(default_factory=list)
    hop_trades: list[SimTrade] = field(default_factory=list)
    skip_counts: dict[str, int] = field(default_factory=dict)
    net_by_delay: dict[int, float] = field(default_factory=dict)


@dataclass
class RunResult:
    run_id: str
    spec: BacktestSpec
    wallets: list[WalletRunResult] = field(default_factory=list)
    pooled: TradeSummary | None = None
    pooled_pessimistic: TradeSummary | None = None
    pooled_optimistic: TradeSummary | None = None
    net_by_delay: dict[int, float] = field(default_factory=dict)
    latency_ratio: float | None = None
    ladder_vs_mirror: PairedComparison | None = None
    ladder_vs_hop: PairedComparison | None = None
    hop_summary: TradeSummary | None = None
    hop_bot_speed_summary: TradeSummary | None = None
    plateau: list[tuple[float, float, float | None]] = field(default_factory=list)
    floor_sensitivity: dict[float, TradeSummary] = field(default_factory=dict)
    n_coarse_trades: int = 0
    elapsed_sec: float = 0.0


def _base_params(cfg: BacktestConfig) -> SimParams:
    return SimParams(
        confirm_delay_sec=PRIMARY_DELAY_SEC,
        fill_model="realistic",
        exit_engine="atr_ladder",
        taker_fee_bps=cfg.taker_fee_bps,
        slippage_bps=cfg.slippage_bps,
    )


class BacktestRunner:
    def __init__(
        self,
        *,
        feed: BacktestDataFeed,
        store: BacktestStore,
        wallet_cfg: WalletCopyConfig,
        backtest_cfg: BacktestConfig,
        blofin_client=None,          # optional: enables the listing gate
        metrics_db=None,             # optional: feeds scout scoring v2
    ):
        self._feed = feed
        self._store = store
        self._wallet_cfg = wallet_cfg
        self._cfg = backtest_cfg
        self._blofin = blofin_client
        self._metrics_db = metrics_db

    async def run(self, spec: BacktestSpec, progress: Progress) -> RunResult:
        started = time.monotonic()
        run_id = f"bt-{uuid.uuid4().hex[:10]}"
        result = RunResult(run_id=run_id, spec=spec)
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - spec.days * 86_400_000

        for wi, address in enumerate(spec.addresses, 1):
            label = short_addr(address)
            await progress(
                f"wallet {wi}/{len(spec.addresses)} {label}: fetching fills...",
            )
            try:
                wr = await self._run_wallet(
                    address, start_ms=start_ms, end_ms=now_ms,
                    progress=progress, wi=wi, n=len(spec.addresses),
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - isolate per wallet
                logger.exception("backtest wallet %s crashed", label)
                wr = WalletRunResult(address=address, error=str(e))
            result.wallets.append(wr)

        self._aggregate(result)
        await self._persist(result)
        # feed the scout's latency-fitness gate: each backtested wallet
        # gets its replayed copier verdict attached for scoring v2
        if self._metrics_db is not None:
            for wr in result.wallets:
                if wr.error or not wr.net_by_delay:
                    continue
                try:
                    await self._metrics_db.record_backtest_fitness(
                        wr.address,
                        latency_ratio=latency_robustness(wr.net_by_delay),
                        copier_net=wr.net_by_delay.get(PRIMARY_DELAY_SEC),
                    )
                except Exception:  # noqa: BLE001 - bookkeeping only
                    logger.warning("backtest fitness writeback failed",
                                   exc_info=True)
        result.elapsed_sec = time.monotonic() - started
        return result

    async def _run_wallet(
        self, address: str, *, start_ms: int, end_ms: int,
        progress: Progress, wi: int, n: int,
    ) -> WalletRunResult:
        wr = WalletRunResult(address=address)
        label = short_addr(address)

        fills, complete = await self._feed.fills(
            address, start_ms=start_ms, end_ms=end_ms,
        )
        wr.n_fills, wr.fills_complete = len(fills), complete
        if not fills:
            wr.error = "no fills in window"
            return wr

        curve = await self._feed.account_value_curve(address, fills)
        stats = compute_fill_stats(
            fills, account_value=curve.at(end_ms), now_ms=end_ms,
        )
        wr.fills_per_day = stats.fills_per_day
        wr.is_scalper = (
            stats.fills_per_day > self._wallet_cfg.scalper_fills_per_day
        )

        events = reconstruct_position_events(fills, account_value_at=curve.at)
        wr.n_events = len(events)
        events, wr.unmapped_coins = await self._filter_listed(events)
        wr.n_open_events = sum(1 for e in events if e.kind in ("open", "flip"))
        if not events:
            wr.error = "no copyable events"
            return wr

        coins = {e.coin for e in events}
        await progress(
            f"wallet {wi}/{n} {label}: {wr.n_open_events} opens, "
            f"{len(coins)} coins; loading candles...",
        )
        market = await self._feed.market_data(
            coins, start_ms=start_ms, end_ms=end_ms,
        )

        base = _base_params(self._cfg)
        cells: list[SimParams] = []
        for d in sorted(set(
            list(self._cfg.delay_grid_sec) + [PRIMARY_DELAY_SEC],
        )):
            cells.append(replace(base, confirm_delay_sec=d))
        cells.append(replace(base, fill_model="optimistic"))
        cells.append(replace(base, fill_model="pessimistic"))
        cells.append(replace(base, exit_engine="mirror"))
        cells.append(replace(base, exit_engine="hop"))
        cells.append(replace(
            base, exit_engine="hop", confirm_delay_sec=BOT_SPEED_DELAY_SEC,
        ))
        for mult in PLATEAU_MULTS:
            if mult != base.atr_mult:
                cells.append(replace(base, atr_mult=mult))
        for floor in FLOOR_SENSITIVITY:
            if floor != base.conviction_floor:
                cells.append(replace(base, conviction_floor=floor))

        for ci, params in enumerate(cells, 1):
            trades, skips = await asyncio.to_thread(
                simulate_wallet, address, events, market, params,
            )
            wr.summaries[params.params_id] = summarize(trades)
            if params == base:
                wr.primary_trades = trades
                for s in skips:
                    wr.skip_counts[s.reason] = wr.skip_counts.get(s.reason, 0) + 1
            elif (
                params.exit_engine == "mirror"
                and params.confirm_delay_sec == PRIMARY_DELAY_SEC
            ):
                wr.mirror_trades = trades
            elif (
                params.exit_engine == "hop"
                and params.confirm_delay_sec == PRIMARY_DELAY_SEC
            ):
                wr.hop_trades = trades
            if (
                params.exit_engine == "atr_ladder"
                and params.fill_model == "realistic"
                and params.atr_mult == base.atr_mult
                and params.conviction_floor == base.conviction_floor
            ):
                wr.net_by_delay[params.confirm_delay_sec] = sum(
                    t.net_pnl_usd for t in trades
                )
            if ci % 5 == 0:
                await progress(
                    f"wallet {wi}/{n} {label}: cell {ci}/{len(cells)} done",
                )
        return wr

    async def _filter_listed(
        self, events: list[LeaderEvent],
    ) -> tuple[list[LeaderEvent], list[str]]:
        """Drop coins Blofin can't mirror (when a client is available)."""
        if self._blofin is None:
            return events, []
        unmapped: list[str] = []
        listed: dict[str, bool] = {}
        for coin in {e.coin for e in events}:
            try:
                info = await self._blofin.resolve_inst_id(
                    hl_coin_to_blofin_base(coin),
                )
                listed[coin] = info is not None
            except Exception:  # noqa: BLE001 - listing check is best-effort
                listed[coin] = True
            if not listed[coin]:
                unmapped.append(coin)
        return [e for e in events if listed.get(e.coin, True)], sorted(unmapped)

    def _aggregate(self, result: RunResult) -> None:
        base = _base_params(self._cfg)
        pooled: list[SimTrade] = []
        for wr in result.wallets:
            pooled.extend(wr.primary_trades)
            for d, net in wr.net_by_delay.items():
                result.net_by_delay[d] = result.net_by_delay.get(d, 0.0) + net
        result.pooled = summarize(pooled)
        result.n_coarse_trades = sum(
            1 for t in pooled if t.resolution == "15m"
        )
        result.latency_ratio = latency_robustness(result.net_by_delay)

        mirror_pool = [t for wr in result.wallets for t in wr.mirror_trades]
        hop_pool = [t for wr in result.wallets for t in wr.hop_trades]
        result.ladder_vs_mirror = paired_exit_comparison(pooled, mirror_pool)
        result.ladder_vs_hop = paired_exit_comparison(pooled, hop_pool)

        # pooled summaries for the named cells, from per-wallet summaries
        def pooled_summary(params_id: str) -> TradeSummary | None:
            merged = TradeSummary()
            found = False
            for wr in result.wallets:
                s = wr.summaries.get(params_id)
                if s is None:
                    continue
                found = True
                merged.n += s.n
                merged.wins += s.wins
                merged.gross_usd += s.gross_usd
                merged.fees_usd += s.fees_usd
                merged.funding_usd += s.funding_usd
                merged.net_usd += s.net_usd
                merged.n_mtm += s.n_mtm
                merged.r_values.extend(s.r_values)
            if not found:
                return None
            if merged.n:
                merged.win_rate = merged.wins / merged.n
                merged.expectancy_usd = merged.net_usd / merged.n
            if merged.r_values:
                merged.avg_r = sum(merged.r_values) / len(merged.r_values)
            merged.ci_suppressed_reason = "pooled from per-wallet summaries"
            return merged

        result.pooled_pessimistic = pooled_summary(
            replace(base, fill_model="pessimistic").params_id,
        )
        result.pooled_optimistic = pooled_summary(
            replace(base, fill_model="optimistic").params_id,
        )
        result.hop_summary = pooled_summary(
            replace(base, exit_engine="hop").params_id,
        )
        result.hop_bot_speed_summary = pooled_summary(
            replace(
                base, exit_engine="hop",
                confirm_delay_sec=BOT_SPEED_DELAY_SEC,
            ).params_id,
        )
        plateau_summaries = {}
        for mult in PLATEAU_MULTS:
            s = pooled_summary(replace(base, atr_mult=mult).params_id)
            if s is not None:
                plateau_summaries[mult] = s
        result.plateau = plateau_table(plateau_summaries)
        for floor in FLOOR_SENSITIVITY:
            s = pooled_summary(
                replace(base, conviction_floor=floor).params_id,
            )
            if s is not None:
                result.floor_sensitivity[floor] = s

    async def _persist(self, result: RunResult) -> None:
        pooled = result.pooled
        await self._store.save_backtest_run(
            result.run_id,
            spec={
                "addresses": result.spec.addresses,
                "days": result.spec.days,
                "label": result.spec.label,
            },
            params={
                "cells": sorted({
                    pid for wr in result.wallets for pid in wr.summaries
                }),
            },
            summary={
                "n": pooled.n if pooled else 0,
                "net_usd": pooled.net_usd if pooled else 0.0,
                "expectancy_usd": pooled.expectancy_usd if pooled else 0.0,
                "latency_ratio": result.latency_ratio,
            },
        )
        primary: list[dict] = []
        for wr in result.wallets:
            for t in wr.primary_trades:
                primary.append({
                    "leader": t.leader, "coin": t.coin, "side": t.side,
                    "event_ts": t.event_ts, "entry_ts": t.entry_ts,
                    "entry_px": t.entry_px, "net": t.net_pnl_usd,
                    "r": t.r_multiple, "resolution": t.resolution,
                    "exits": [
                        {"ts": e.ts_ms, "px": e.px, "f": e.fraction,
                         "why": e.reason}
                        for e in t.exits
                    ],
                })
        await self._store.save_backtest_trades(result.run_id, "primary", primary)
