"""Fills -> leader position events, with provable watcher parity.

The live watcher detects opens/adds/reduces/closes/flips by diffing
successive clearinghouseState snapshots with ``diff_positions``. The
backtester must detect the same events from a historical FILLS stream.
Rather than writing (and inevitably diverging) a second classifier, this
module rebuilds synthetic position snapshots from the fills, bucketed to
the watcher's cadence, and pushes them through the watcher's own
``diff_positions``. The only surface that can drift is snapshot
construction, which the parity test pins.

Bucketing note: the live watcher polls every ~15s, so a position opened
and closed inside one poll is invisible live. Bucketing fills (default
60s = the simulator's candle resolution) reproduces that coalescing; a
same-bucket round trip emits no event, conservatively.

Conviction caveat: fills carry no margin or leverage, so historical
conviction is a NOTIONAL fraction (position notional / account value),
not the live margin fraction. The account-value curve interpolates
between daily archived anchors by walking realized pnl and fees
backwards; deposits/withdrawals inside a day are the residual error.
Reports must label this "notional-conviction proxy" and sweep the floor.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass, field
from typing import Callable

from src.trading.hl_info_client import WalletPosition
from src.trading.wallet_metrics_db import StoredPosition
from src.trading.wallet_watcher import diff_positions

logger = logging.getLogger(__name__)

_DIR_KIND_HINTS = {
    "open": ("Open Long", "Open Short"),
    "close": ("Close Long", "Close Short"),
}


@dataclass(frozen=True)
class LeaderEvent:
    """One visible position change, in the watcher's delta vocabulary."""

    ts_ms: int                 # bucket end = when the change becomes visible
    kind: str                  # 'open' | 'add' | 'reduce' | 'close' | 'flip'
    coin: str
    prev_szi: float
    curr_szi: float
    fill_vwap: float           # size-weighted price of the bucket's fills
    notional: float            # abs(curr_szi) * fill_vwap (0 for closes)
    conviction: float          # notional / account_value at ts (clamped 0..1)
    reduce_fraction: float     # for reduce: (|prev|-|curr|)/|prev|, else 0


@dataclass
class AccountValueCurve:
    """Daily account-value anchors + fill-walk interpolation.

    anchors: sorted list of (ts_ms, account_value) from archived
    leaderboard/wallet-state snapshots. Between (and before) anchors the
    value is estimated by subtracting realized pnl net of fees accrued
    AFTER the queried time from the next anchor: av(t) = anchor_av -
    sum(closedPnl - fee over fills in (t, anchor_t]). Past the last
    anchor it walks forward from it. With daily anchors the residual
    error (deposits/withdrawals) is bounded by one day.
    """

    anchors: list[tuple[int, float]] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)
    fallback: float = 0.0      # used when there are no anchors at all

    def __post_init__(self) -> None:
        self.anchors = sorted(self.anchors)
        self.fills = sorted(self.fills, key=lambda f: int(f.get("time", 0)))
        self._fill_times = [int(f.get("time", 0)) for f in self.fills]
        # cumulative net realized pnl up to and including fill i
        cum = 0.0
        self._cum_net: list[float] = []
        for f in self.fills:
            cum += _net_pnl(f)
            self._cum_net.append(cum)

    def _net_between(self, t0: int, t1: int) -> float:
        """Sum of (closedPnl - fee) over fills with t0 < time <= t1."""
        if not self.fills or t1 <= t0:
            return 0.0
        hi = bisect.bisect_right(self._fill_times, t1) - 1
        lo = bisect.bisect_right(self._fill_times, t0) - 1
        upper = self._cum_net[hi] if hi >= 0 else 0.0
        lower = self._cum_net[lo] if lo >= 0 else 0.0
        return upper - lower

    def at(self, ts_ms: int) -> float:
        if not self.anchors:
            return self.fallback
        idx = bisect.bisect_left(self.anchors, (ts_ms, float("-inf")))
        if idx < len(self.anchors):
            anchor_ts, anchor_av = self.anchors[idx]      # next anchor at/after t
            return max(0.0, anchor_av - self._net_between(ts_ms, anchor_ts))
        anchor_ts, anchor_av = self.anchors[-1]           # walk forward
        return max(0.0, anchor_av + self._net_between(anchor_ts, ts_ms))


def _net_pnl(fill: dict) -> float:
    try:
        closed = float(fill.get("closedPnl", 0.0))
    except (TypeError, ValueError):
        closed = 0.0
    try:
        fee = float(fill.get("fee", 0.0))
    except (TypeError, ValueError):
        fee = 0.0
    return closed - fee


def _signed_size(fill: dict) -> float:
    try:
        sz = float(fill.get("sz", 0.0))
    except (TypeError, ValueError):
        return 0.0
    side = str(fill.get("side", "")).upper()
    return sz if side in ("B", "BUY") else -sz


def reconstruct_position_events(
    fills: list[dict],
    *,
    bucket_ms: int = 60_000,
    account_value_at: Callable[[int], float],
) -> list[LeaderEvent]:
    """Replay fills into watcher-vocabulary position events.

    Walks fills in time order, maintaining per-coin running positions
    seeded from each coin's first ``startPosition``. At every bucket
    boundary where anything changed, materializes prev/curr snapshots and
    classifies via ``diff_positions``. Returns events ordered by time.
    """
    if not fills:
        return []
    fills = sorted(fills, key=lambda f: int(f.get("time", 0)))

    # Seed positions from each coin's first-seen startPosition so a window
    # that begins mid-position doesn't fake an 'open'.
    positions: dict[str, float] = {}
    entry_px: dict[str, float] = {}
    for f in fills:
        coin = str(f.get("coin", "")).strip()
        if coin and coin not in positions:
            try:
                positions[coin] = float(f.get("startPosition", 0.0))
            except (TypeError, ValueError):
                positions[coin] = 0.0
            try:
                entry_px[coin] = float(f.get("px", 0.0))
            except (TypeError, ValueError):
                entry_px[coin] = 0.0

    def snapshot_stored(pos: dict[str, float]) -> dict[str, StoredPosition]:
        return {
            c: StoredPosition(coin=c, szi=s, entry_px=entry_px.get(c, 0.0))
            for c, s in pos.items() if s != 0.0
        }

    events: list[LeaderEvent] = []
    dir_mismatches = 0

    prev_positions = dict(positions)
    bucket_start = (int(fills[0].get("time", 0)) // bucket_ms) * bucket_ms
    bucket_fills: list[dict] = []

    def flush(bucket_end: int) -> None:
        nonlocal prev_positions, dir_mismatches
        if not bucket_fills:
            return
        prev_snap = snapshot_stored(prev_positions)
        curr_snap = {
            c: WalletPosition(
                coin=c, szi=s, entry_px=entry_px.get(c, 0.0),
                leverage=0.0, notional=0.0, margin_used=0.0,
            )
            for c, s in positions.items() if s != 0.0
        }
        deltas = diff_positions(prev_snap, curr_snap)
        # bucket VWAP per coin
        vwap: dict[str, float] = {}
        for c in {str(f.get("coin", "")).strip() for f in bucket_fills}:
            num = den = 0.0
            for f in bucket_fills:
                if str(f.get("coin", "")).strip() != c:
                    continue
                try:
                    px = float(f.get("px", 0.0))
                    sz = abs(float(f.get("sz", 0.0)))
                except (TypeError, ValueError):
                    continue
                num += px * sz
                den += sz
            vwap[c] = num / den if den > 0 else 0.0
        for d in deltas:
            px = vwap.get(d.coin, entry_px.get(d.coin, 0.0))
            notional = abs(d.curr_szi) * px
            av = account_value_at(bucket_end)
            conviction = min(1.0, max(0.0, notional / av)) if av > 0 else 0.0
            reduce_fraction = 0.0
            if d.kind == "reduce" and d.prev_szi:
                reduce_fraction = (
                    (abs(d.prev_szi) - abs(d.curr_szi)) / abs(d.prev_szi)
                )
            events.append(LeaderEvent(
                ts_ms=bucket_end, kind=d.kind, coin=d.coin,
                prev_szi=d.prev_szi, curr_szi=d.curr_szi,
                fill_vwap=px, notional=notional, conviction=conviction,
                reduce_fraction=reduce_fraction,
            ))
            # canary: does the exchange's own dir string agree?
            hints = _DIR_KIND_HINTS.get(d.kind)
            if hints:
                bucket_dirs = {
                    str(f.get("dir", "")) for f in bucket_fills
                    if str(f.get("coin", "")).strip() == d.coin
                }
                if bucket_dirs and not any(
                    h in bd for bd in bucket_dirs for h in hints
                ):
                    dir_mismatches += 1
        prev_positions = dict(positions)
        bucket_fills.clear()

    for f in fills:
        t = int(f.get("time", 0))
        coin = str(f.get("coin", "")).strip()
        if not coin:
            continue
        while t >= bucket_start + bucket_ms:
            flush(bucket_start + bucket_ms)
            bucket_start += bucket_ms
        new_pos = positions.get(coin, 0.0) + _signed_size(f)
        if abs(new_pos) < 1e-12:
            new_pos = 0.0
        positions[coin] = new_pos
        try:
            px = float(f.get("px", 0.0))
            if px > 0:
                entry_px[coin] = px
        except (TypeError, ValueError):
            pass
        bucket_fills.append(f)
    flush(bucket_start + bucket_ms)

    if dir_mismatches:
        logger.warning(
            "position reconstruction: %d dir-string disagreement(s); "
            "possible fills schema drift", dir_mismatches,
        )
    return events
