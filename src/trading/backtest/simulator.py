"""Pure copy-trade replay engine.

Simulates the COPIER's PnL (not the leader's) for a stream of
LeaderEvents under our copy rules, against 1m candles. Pure functions
over in-memory data: no IO, no clock, fully deterministic, so every exit
rule is unit-testable on hand-built candles.

Conservatism rules (each of these alone can flip a paper result, so they
are fixed here and NOT configurable):
  - entries fill at the next 1m open at/after (event + confirm delay),
    never at the leader's price except in the optimistic bound model;
  - exits resolve on 1m candles with the gap rule (an open already beyond
    a level fills at the open, not the level);
  - when the stop and any take-profit both fit inside one candle, the
    STOP fills first for the whole remaining position;
  - every partial pays its own taker fee; funding accrues hourly.

Three exit engines share identical entries so exit policy is a paired
comparison, never a re-selection:
  atr_ladder  live rules: ATR stop + weighted 1R/2R/3R ladder, leader
              close still forces the remainder out (Luke reacts to a DM)
  mirror      exit only when the leader reduces (proportional) or closes
  hop         the trade-hopping experiment: quick TP at +hop_tp_r x risk
              or a time stop, whichever first; leader is ignored
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from src.trading.backtest.data_store import Candle
from src.trading.backtest.position_events import LeaderEvent
from src.trading.venue import DEFAULT_TP_WEIGHTS
from src.trading.wallet_watcher import compute_atr

FILL_MODELS = ("optimistic", "realistic", "pessimistic")
EXIT_ENGINES = ("atr_ladder", "mirror", "hop")

# Normalized stake per simulated trade. Expectancy is reported per $1k
# notional so wallets and parameter cells are directly comparable.
NOTIONAL_USD = 1_000.0

_MINUTE_MS = 60_000
_HOUR_MS = 3_600_000


@dataclass(frozen=True)
class SimParams:
    confirm_delay_sec: int = 120
    fill_model: str = "realistic"
    exit_engine: str = "atr_ladder"
    taker_fee_bps: float = 6.0
    slippage_bps: float = 10.0
    atr_period: int = 14
    atr_mult: float = 1.5
    tp_weights: tuple[float, ...] = DEFAULT_TP_WEIGHTS
    rr_targets: tuple[float, ...] = (1.0, 2.0, 3.0)
    hop_tp_r: float = 0.5
    hop_time_stop_min: float = 30.0
    leader_close_forces_exit: bool = True
    max_hold_days: float = 14.0
    conviction_floor: float = 0.25   # notional-conviction proxy floor
    cooldown_min: float = 30.0

    @property
    def params_id(self) -> str:
        return (
            f"{self.exit_engine}|d{self.confirm_delay_sec}s|{self.fill_model}"
            f"|atr{self.atr_mult:g}|slip{self.slippage_bps:g}"
        )


@dataclass(frozen=True)
class Execution:
    ts_ms: int
    px: float
    fraction: float          # of the ORIGINAL position size
    reason: str              # tp1|tp2|tp3|sl|leader_exit|hop_tp|time_stop|mtm
    fee_usd: float


@dataclass
class SimTrade:
    leader: str
    coin: str
    side: str                # LONG | SHORT
    event_ts: int
    entry_ts: int
    entry_px: float
    stop_px: float | None
    tp_prices: list[float]
    exits: list[Execution] = field(default_factory=list)
    gross_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    funding_usd: float = 0.0
    net_pnl_usd: float = 0.0
    r_multiple: float | None = None
    resolution: str = "1m"   # '1m' | 'mtm' (unresolved, marked to market)
    params_id: str = ""


@dataclass(frozen=True)
class SkippedEvent:
    ts_ms: int
    coin: str
    reason: str              # conviction_floor|cooldown|no_candles|no_atr_entry


@dataclass
class MarketData:
    """Pre-fetched, per-coin market history the simulator reads from.

    candles_1m may be a SPLICED series: native 1m where archived, 15m
    candles filling older gaps (the archive only accumulates 1m going
    forward). coarse_ranges lists the [start, end) spans that came from
    15m so trades entered there are honesty-flagged resolution='15m'
    (wider candles = more SL-first conservatism, not more precision).
    """

    candles_1m: dict[str, list[Candle]] = field(default_factory=dict)
    candles_1h: dict[str, list[Candle]] = field(default_factory=dict)
    funding: dict[str, list[tuple[int, float]]] = field(default_factory=dict)
    coarse_ranges: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    def is_coarse(self, coin: str, ts_ms: int) -> bool:
        for start, end in self.coarse_ranges.get(coin) or []:
            if start <= ts_ms < end:
                return True
        return False


def _first_at_or_after(candles: list[Candle], ts_ms: int) -> int:
    """Index of the first candle whose open time >= ts_ms, or len()."""
    return bisect.bisect_left([c.ts for c in candles], ts_ms)


def atr_at(
    candles_1h: list[Candle], ts_ms: int, period: int,
) -> float | None:
    """ATR from CLOSED 1h candles strictly before ts_ms (no lookahead)."""
    closed = [c for c in candles_1h if c.ts + _HOUR_MS <= ts_ms]
    rows = [
        {"h": str(c.h), "l": str(c.l), "c": str(c.c)}
        for c in closed[-(period + 20):]
    ]
    return compute_atr(rows, period)


def funding_cost_usd(
    funding: list[tuple[int, float]], *, is_long: bool,
    notional_open: list[tuple[int, int, float]],
) -> float:
    """Funding paid over the hold. notional_open = [(start, end, notional)]
    spans of remaining notional. Positive result = cost to us."""
    total = 0.0
    for ts, rate in funding:
        for start, end, notional in notional_open:
            if start <= ts < end:
                total += rate * notional if is_long else -rate * notional
                break
    return total


def _entry_price(
    candle: Candle, *, model: str, is_long: bool,
    leader_px: float, slippage_bps: float,
) -> float:
    slip = slippage_bps / 10_000.0
    if model == "optimistic":
        return leader_px if leader_px > 0 else candle.o
    if model == "pessimistic":
        return candle.h if is_long else candle.l
    px = candle.o
    return px * (1 + slip) if is_long else px * (1 - slip)


@dataclass
class _OpenState:
    is_long: bool
    entry_px: float
    stop_px: float | None
    # remaining TP legs as (price, fraction-of-original)
    tp_legs: list[tuple[float, float]]
    remaining: float = 1.0


def resolve_candle(state: _OpenState, candle: Candle) -> list[Execution]:
    """Resolve SL/TP touches inside one 1m candle. Pure kernel.

    Gap rule first: an open already at/beyond a level fills at the open.
    Ambiguity rule: if the stop and any remaining TP are both inside the
    candle's range, the stop fills FIRST for the whole remainder.
    Fees are attached by the caller (fraction * notional is known there).
    """
    execs: list[Execution] = []
    if state.remaining <= 0:
        return execs
    long = state.is_long

    def stop_hit_at(px: float) -> None:
        execs.append(Execution(
            ts_ms=candle.ts, px=px, fraction=state.remaining,
            reason="sl", fee_usd=0.0,
        ))
        state.remaining = 0.0
        state.tp_legs = []

    sl = state.stop_px
    # --- gap through the stop at the open: worst case first ---
    if sl is not None and (candle.o <= sl if long else candle.o >= sl):
        stop_hit_at(candle.o)
        return execs
    # --- gap through TPs at the open ---
    still: list[tuple[float, float]] = []
    for price, frac in state.tp_legs:
        gapped = candle.o >= price if long else candle.o <= price
        if gapped:
            execs.append(Execution(
                ts_ms=candle.ts, px=candle.o, fraction=frac,
                reason=f"tp{len(execs) + 1}", fee_usd=0.0,
            ))
            state.remaining = max(0.0, state.remaining - frac)
        else:
            still.append((price, frac))
    state.tp_legs = still
    if state.remaining <= 0:
        return execs

    sl_in_range = sl is not None and (candle.l <= sl if long else candle.h >= sl)
    tp_in_range = [
        (price, frac) for price, frac in state.tp_legs
        if (candle.h >= price if long else candle.l <= price)
    ]
    if sl_in_range:
        # conservative: the stop eats the remainder before any same-candle TP
        stop_hit_at(sl)  # type: ignore[arg-type]
        return execs
    if tp_in_range:
        remaining_legs = []
        for price, frac in state.tp_legs:
            if (price, frac) in tp_in_range:
                execs.append(Execution(
                    ts_ms=candle.ts, px=price, fraction=frac,
                    reason="tp", fee_usd=0.0,
                ))
                state.remaining = max(0.0, state.remaining - frac)
            else:
                remaining_legs.append((price, frac))
        state.tp_legs = remaining_legs
    return execs


def _tp_ladder(
    entry: float, *, is_long: bool, risk: float,
    rr_targets: tuple[float, ...], weights: tuple[float, ...],
) -> list[tuple[float, float]]:
    w = list(weights[: len(rr_targets)]) or [1.0]
    wsum = sum(w)
    legs = []
    for i, rr in enumerate(rr_targets[: len(w)]):
        px = entry + rr * risk if is_long else entry - rr * risk
        if px > 0:
            legs.append((px, w[i] / wsum))
    return legs


def simulate_wallet(
    leader: str,
    events: list[LeaderEvent],
    market: MarketData,
    params: SimParams,
) -> tuple[list[SimTrade], list[SkippedEvent]]:
    """Replay one wallet's events under our copy rules. Deterministic."""
    trades: list[SimTrade] = []
    skips: list[SkippedEvent] = []
    last_entry_by_coin: dict[str, int] = {}
    delay_ms = params.confirm_delay_sec * 1000
    max_hold_ms = int(params.max_hold_days * 86_400_000)

    events = sorted(events, key=lambda e: e.ts_ms)
    # exit-relevant events per coin for mirror/forced exits
    exit_events: dict[str, list[LeaderEvent]] = {}
    for e in events:
        if e.kind in ("reduce", "close", "flip"):
            exit_events.setdefault(e.coin, []).append(e)

    for e in events:
        if e.kind not in ("open", "flip"):
            continue
        if e.conviction < params.conviction_floor:
            skips.append(SkippedEvent(e.ts_ms, e.coin, "conviction_floor"))
            continue
        last = last_entry_by_coin.get(e.coin)
        if last is not None and e.ts_ms - last < params.cooldown_min * 60_000:
            skips.append(SkippedEvent(e.ts_ms, e.coin, "cooldown"))
            continue

        candles = market.candles_1m.get(e.coin) or []
        exec_ts = e.ts_ms + delay_ms
        idx = _first_at_or_after(candles, exec_ts)
        if idx >= len(candles):
            skips.append(SkippedEvent(e.ts_ms, e.coin, "no_candles"))
            continue
        entry_candle = candles[idx]
        is_long = e.curr_szi > 0
        atr = atr_at(
            market.candles_1h.get(e.coin) or [], entry_candle.ts,
            params.atr_period,
        )
        if atr is None and params.exit_engine != "mirror":
            skips.append(SkippedEvent(e.ts_ms, e.coin, "no_atr_entry"))
            continue

        entry_px = _entry_price(
            entry_candle, model=params.fill_model, is_long=is_long,
            leader_px=e.fill_vwap, slippage_bps=params.slippage_bps,
        )
        last_entry_by_coin[e.coin] = e.ts_ms
        risk = (atr or 0.0) * params.atr_mult
        size_units = NOTIONAL_USD / entry_px

        if params.exit_engine == "atr_ladder":
            stop_px = entry_px - risk if is_long else entry_px + risk
            if stop_px <= 0:
                stop_px = None
            tp_legs = _tp_ladder(
                entry_px, is_long=is_long, risk=risk,
                rr_targets=params.rr_targets, weights=params.tp_weights,
            )
        elif params.exit_engine == "hop":
            stop_px = entry_px - risk if is_long else entry_px + risk
            if stop_px <= 0:
                stop_px = None
            hop_px = (
                entry_px + params.hop_tp_r * risk if is_long
                else entry_px - params.hop_tp_r * risk
            )
            tp_legs = [(hop_px, 1.0)] if hop_px > 0 else []
        else:  # mirror
            stop_px, tp_legs = None, []

        trade = SimTrade(
            leader=leader, coin=e.coin, side="LONG" if is_long else "SHORT",
            event_ts=e.ts_ms, entry_ts=entry_candle.ts, entry_px=entry_px,
            stop_px=stop_px, tp_prices=[p for p, _ in tp_legs],
            params_id=params.params_id,
            resolution="15m" if market.is_coarse(e.coin, entry_candle.ts) else "1m",
        )
        state = _OpenState(
            is_long=is_long, entry_px=entry_px, stop_px=stop_px,
            tp_legs=list(tp_legs),
        )

        # forced-exit schedule from the leader's own exits
        forced: list[tuple[int, float, str]] = []   # (exec ts, fraction-of-current, reason)
        if params.exit_engine == "mirror" or (
            params.exit_engine == "atr_ladder" and params.leader_close_forces_exit
        ):
            for xe in exit_events.get(e.coin, []):
                if xe.ts_ms <= e.ts_ms:
                    continue
                if xe.kind in ("close", "flip"):
                    forced.append((xe.ts_ms + delay_ms, 1.0, "leader_exit"))
                    break   # nothing to mirror after a full close
                if params.exit_engine == "mirror" and xe.kind == "reduce":
                    forced.append(
                        (xe.ts_ms + delay_ms, xe.reduce_fraction, "leader_exit"),
                    )

        deadline_ms = entry_candle.ts + max_hold_ms
        if params.exit_engine == "hop":
            deadline_ms = min(
                deadline_ms,
                entry_candle.ts + int(params.hop_time_stop_min * 60_000),
            )

        # --- walk candles ---
        fi = 0
        i = idx
        while i < len(candles) and state.remaining > 0:
            c = candles[i]
            # forced exits due at/before this candle fill at its open
            while fi < len(forced) and forced[fi][0] <= c.ts:
                _, frac_of_current, reason = forced[fi]
                fraction = state.remaining * min(1.0, frac_of_current)
                if fraction > 0:
                    trade.exits.append(Execution(
                        ts_ms=c.ts, px=c.o, fraction=fraction,
                        reason=reason, fee_usd=0.0,
                    ))
                    state.remaining = max(0.0, state.remaining - fraction)
                fi += 1
            if state.remaining <= 0:
                break
            if c.ts >= deadline_ms:
                reason = (
                    "time_stop" if params.exit_engine == "hop"
                    and deadline_ms < entry_candle.ts + max_hold_ms else "mtm"
                )
                trade.exits.append(Execution(
                    ts_ms=c.ts, px=c.o, fraction=state.remaining,
                    reason=reason, fee_usd=0.0,
                ))
                if reason == "mtm":
                    trade.resolution = "mtm"
                state.remaining = 0.0
                break
            trade.exits.extend(resolve_candle(state, c))
            i += 1

        if state.remaining > 0:
            # ran out of candles: mark to market at the last close
            last_c = candles[-1]
            trade.exits.append(Execution(
                ts_ms=last_c.ts, px=last_c.c, fraction=state.remaining,
                reason="mtm", fee_usd=0.0,
            ))
            trade.resolution = "mtm"
            state.remaining = 0.0

        _settle(trade, size_units, params, market, risk)
        trades.append(trade)
    return trades, skips


def _settle(
    trade: SimTrade, size_units: float, params: SimParams,
    market: MarketData, risk: float,
) -> None:
    """Fill in pnl, fees, funding and the R multiple."""
    fee_rate = params.taker_fee_bps / 10_000.0
    long = trade.side == "LONG"
    gross = 0.0
    fees = NOTIONAL_USD * fee_rate            # entry fee
    exits = []
    for ex in trade.exits:
        exit_notional = ex.fraction * NOTIONAL_USD
        fee = exit_notional * fee_rate
        fees += fee
        move = (ex.px - trade.entry_px) if long else (trade.entry_px - ex.px)
        gross += move * size_units * ex.fraction
        exits.append(Execution(
            ts_ms=ex.ts_ms, px=ex.px, fraction=ex.fraction,
            reason=ex.reason, fee_usd=fee,
        ))
    trade.exits = exits

    spans = []
    remaining = 1.0
    span_start = trade.entry_ts
    for ex in trade.exits:
        spans.append((span_start, ex.ts_ms, remaining * NOTIONAL_USD))
        remaining = max(0.0, remaining - ex.fraction)
        span_start = ex.ts_ms
        if remaining <= 0:
            break
    funding = funding_cost_usd(
        market.funding.get(trade.coin) or [], is_long=long,
        notional_open=spans,
    )

    trade.gross_pnl_usd = gross
    trade.fees_usd = fees
    trade.funding_usd = funding
    trade.net_pnl_usd = gross - fees - funding
    risk_usd = risk * size_units
    trade.r_multiple = (
        trade.net_pnl_usd / risk_usd if risk_usd > 0 else None
    )
