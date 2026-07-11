"""Nightly wallet scout: find + maintain the best wallets to copy.

Pipeline (run_once):

  1. Pull the Hyperliquid leaderboard (~40k rows).
  2. Screen on window consistency, account size, allTime-vs-month pnl and
     volume/account (excludes market makers).
  3. VERIFY finalists at trade level by replaying their recent userFills:
     win rate, profit factor, max drawdown, median hold, fills/day,
     top-trade concentration, and position-size-vs-equity (conviction).
  4. Score copyability 0-100 (not raw ROI): scalpers, dormant wallets,
     one-lucky-trade months and non-Blofin coin mixes are penalised.
  5. Persist metrics history, then promote/demote the tracked set (max N)
     with streak-based hysteresis so a single good or bad night never
     churns the set.
  6. DM a digest of any changes to the allowlist. Never swaps silently.

Everything that decides is a pure function (screen_leaderboard,
compute_fill_stats, copyability_score, apply_hysteresis) so the whole
brain is unit-testable without network.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Iterable

from src.config.settings import WalletCopyConfig
from src.trading.hl_info_client import HLInfoError, HyperliquidInfoClient, LeaderboardRow
from src.trading.wallet_metrics_db import (
    TrackedWallet,
    WalletMetrics,
    WalletMetricsDB,
)

logger = logging.getLogger(__name__)

SendDM = Callable[[int, str], Awaitable[None]]

# Pause between per-wallet verification fetches; keeps us polite on the
# public info endpoint (the nightly job is in no hurry).
_VERIFY_PACE_SEC = 0.7


def short_addr(address: str) -> str:
    a = address.strip()
    return f"{a[:6]}..{a[-4:]}" if len(a) > 12 else a


def hl_coin_to_blofin_base(coin: str) -> str:
    """Map a Hyperliquid coin symbol to the Blofin base symbol.

    Hyperliquid prefixes thousand-lots with 'k' (kPEPE); Blofin lists the
    same market as 1000PEPE. Everything else maps 1:1.
    """
    c = coin.strip()
    if len(c) > 1 and c[0] == "k" and c[1:].isupper():
        return "1000" + c[1:]
    return c.upper()


# ---------------------------------------------------------------------------
# 1-2. screen
# ---------------------------------------------------------------------------


def screen_leaderboard(
    rows: Iterable[LeaderboardRow], cfg: WalletCopyConfig,
) -> list[LeaderboardRow]:
    """First-pass screen, cheap fields only (no per-wallet fetches).

    Keeps wallets with consistent profit across week/month/allTime, an
    account big enough to matter but small enough to copy, an allTime pnl
    that dwarfs the current month (not a one-month wonder), and a
    volume/account ratio low enough to exclude market makers/HFT.
    """
    out: list[LeaderboardRow] = []
    for r in rows:
        if not (cfg.min_account_value <= r.account_value <= cfg.max_account_value):
            continue
        week = r.pnl.get("week", 0.0)
        month = r.pnl.get("month", 0.0)
        alltime = r.pnl.get("allTime", 0.0)
        if week <= 0 or month <= 0 or alltime <= 0:
            continue
        if alltime < cfg.alltime_month_factor * month:
            continue
        volume = r.volume.get("allTime", 0.0) or r.volume.get("month", 0.0)
        if r.account_value > 0 and volume / r.account_value > cfg.max_volume_ratio:
            continue
        out.append(r)
    # Rank by month ROI: recent, normalised edge first.
    out.sort(key=lambda r: r.roi.get("month", 0.0), reverse=True)
    return out


# ---------------------------------------------------------------------------
# 3. trade-level verification
# ---------------------------------------------------------------------------


@dataclass
class FillStats:
    """Replay of a wallet's recent fills, aggregated per closed episode."""

    n_fills: int = 0
    span_days: float = 0.0
    fills_per_day: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_usd: float = 0.0
    median_hold_min: float = 0.0
    conviction_median: float = 0.0     # episode max notional / account value
    top_trade_share: float = 0.0       # best episode pnl / total positive pnl
    hours_since_fill: float = 0.0
    coins: set[str] = field(default_factory=set)
    n_episodes: int = 0


def _fill_signed_size(fill: dict) -> float:
    """Signed base-unit size: buys positive, sells negative."""
    try:
        sz = float(fill.get("sz", 0.0))
    except (TypeError, ValueError):
        return 0.0
    side = str(fill.get("side", "")).upper()
    return sz if side in ("B", "BUY") else -sz


def compute_fill_stats(
    fills: list[dict], *, account_value: float, now_ms: int | None = None,
) -> FillStats:
    """Reconstruct per-coin position episodes from a userFills list.

    An episode runs from flat (position 0) to flat again; a sign flip ends
    one episode and starts the next. Only closed episodes contribute pnl,
    win-rate and hold-time stats; every episode contributes conviction
    (its peak notional as a fraction of account value).
    """
    stats = FillStats()
    if not fills:
        return stats
    now_ms = now_ms or int(time.time() * 1000)
    fills = sorted(fills, key=lambda f: int(f.get("time", 0)))
    times = [int(f.get("time", 0)) for f in fills]
    stats.n_fills = len(fills)
    stats.span_days = max((times[-1] - times[0]) / 86_400_000.0, 1e-9)
    stats.fills_per_day = stats.n_fills / max(stats.span_days, 1.0)
    stats.hours_since_fill = max(0.0, (now_ms - times[-1]) / 3_600_000.0)

    # --- episode reconstruction, per coin ---
    by_coin: dict[str, list[dict]] = {}
    for f in fills:
        coin = str(f.get("coin", "")).strip()
        if coin:
            by_coin.setdefault(coin, []).append(f)
    stats.coins = set(by_coin)

    episode_pnls: list[float] = []
    episode_holds_min: list[float] = []
    convictions: list[float] = []
    cum = 0.0
    peak = 0.0
    max_dd = 0.0

    for coin, coin_fills in by_coin.items():
        pos = 0.0
        try:
            pos = float(coin_fills[0].get("startPosition", 0.0))
        except (TypeError, ValueError):
            pos = 0.0
        ep_open = pos != 0.0
        ep_start = int(coin_fills[0].get("time", 0)) if ep_open else 0
        ep_pnl = 0.0
        ep_peak_notional = 0.0
        # A pre-existing open position means the first episode started
        # before our window; it still closes normally.
        for f in coin_fills:
            t = int(f.get("time", 0))
            delta = _fill_signed_size(f)
            try:
                px = float(f.get("px", 0.0))
            except (TypeError, ValueError):
                px = 0.0
            try:
                closed = float(f.get("closedPnl", 0.0))
            except (TypeError, ValueError):
                closed = 0.0
            prev = pos
            pos = prev + delta
            if abs(pos) < 1e-12:
                pos = 0.0
            ep_pnl += closed
            if prev == 0.0 and pos != 0.0:
                ep_open, ep_start = True, t
                ep_pnl, ep_peak_notional = closed, abs(pos) * px
                continue
            ep_peak_notional = max(ep_peak_notional, abs(pos) * px, abs(prev) * px)
            flipped = prev != 0.0 and pos != 0.0 and (prev > 0) != (pos > 0)
            if ep_open and (pos == 0.0 or flipped):
                episode_pnls.append(ep_pnl)
                episode_holds_min.append(max(0.0, (t - ep_start) / 60_000.0))
                if account_value > 0:
                    convictions.append(ep_peak_notional / account_value)
                ep_pnl = 0.0
                ep_peak_notional = abs(pos) * px
                ep_open = pos != 0.0
                ep_start = t
        # still-open episode: conviction only
        if ep_open and account_value > 0 and ep_peak_notional > 0:
            convictions.append(ep_peak_notional / account_value)

    stats.n_episodes = len(episode_pnls)
    if episode_pnls:
        wins = [p for p in episode_pnls if p > 0]
        losses = [p for p in episode_pnls if p < 0]
        stats.win_rate = len(wins) / len(episode_pnls)
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        stats.profit_factor = (
            gross_win / gross_loss if gross_loss > 0
            else (10.0 if gross_win > 0 else 0.0)
        )
        if gross_win > 0:
            stats.top_trade_share = max(wins) / gross_win
        for p in episode_pnls:
            cum += p
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)
        stats.max_drawdown_usd = max_dd
    if episode_holds_min:
        stats.median_hold_min = statistics.median(episode_holds_min)
    if convictions:
        stats.conviction_median = statistics.median(convictions)
    return stats


# ---------------------------------------------------------------------------
# 4. copyability score
# ---------------------------------------------------------------------------


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def copyability_score(
    row: LeaderboardRow, stats: FillStats, *, blofin_coverage: float,
    cfg: WalletCopyConfig,
) -> float:
    """0-100. How copyable is this wallet, not how profitable.

    Weighted components; then hard multiplicative penalties for the two
    patterns that make copying structurally bad (scalping: our confirm
    latency eats the edge; dormancy: nothing to copy).
    """
    if stats.n_episodes < cfg.min_episodes:
        return 0.0

    # consistency across pnl windows (20)
    windows = [row.pnl.get("day", 0.0), row.pnl.get("week", 0.0),
               row.pnl.get("month", 0.0)]
    consistency = sum(1.0 for w in windows if w > 0) / len(windows)

    # trade quality: win rate + profit factor (25)
    wr = _clamp01((stats.win_rate - 0.40) / 0.25)          # 40% -> 0, 65% -> 1
    pf = _clamp01((stats.profit_factor - 1.0) / 1.5)       # 1.0 -> 0, 2.5 -> 1
    quality = 0.5 * wr + 0.5 * pf

    # drawdown vs account (15): 0% -> 1, >= 30% of account -> 0
    dd_pct = (
        100.0 * stats.max_drawdown_usd / row.account_value
        if row.account_value > 0 else 100.0
    )
    drawdown = _clamp01(1.0 - dd_pct / 30.0)

    # activity fit (15): enough flow to copy, not scalping
    if stats.fills_per_day <= 0:
        activity = 0.0
    elif stats.fills_per_day <= 10.0:
        activity = 1.0
    else:
        activity = _clamp01(
            1.0 - (stats.fills_per_day - 10.0)
            / max(cfg.scalper_fills_per_day - 10.0, 1.0)
        )

    # conviction (10): sized positions, not dust punts
    conviction = _clamp01(stats.conviction_median / 0.20)   # 20% of equity -> 1

    # venue coverage (10): share of their coins we can actually mirror
    coverage = _clamp01(blofin_coverage)

    # luck (5): pnl not dominated by one trade
    luck = _clamp01((0.8 - stats.top_trade_share) / 0.4)    # <=0.4 -> 1, >=0.8 -> 0

    score = 100.0 * (
        0.20 * consistency + 0.25 * quality + 0.15 * drawdown
        + 0.15 * activity + 0.10 * conviction + 0.10 * coverage + 0.05 * luck
    )
    if stats.fills_per_day > cfg.scalper_fills_per_day:
        score *= 0.3
    if stats.hours_since_fill > cfg.dormant_hours:
        score *= 0.5
    return round(score, 2)


# ---------------------------------------------------------------------------
# 5. hysteresis
# ---------------------------------------------------------------------------


@dataclass
class HysteresisResult:
    promoted: list[str] = field(default_factory=list)
    demoted: list[str] = field(default_factory=list)
    wallets: dict[str, TrackedWallet] = field(default_factory=dict)


def apply_hysteresis(
    existing: dict[str, TrackedWallet],
    today: dict[str, tuple[float, bool]],   # address -> (score, is_scalper)
    cfg: WalletCopyConfig,
) -> HysteresisResult:
    """Streak-based promote/demote. Pure; the caller persists + DMs.

    Promotion needs `promote_streak` consecutive nights at/above
    promote_score. Demotion needs `demote_streak` consecutive nights below
    demote_score. A full tracked set only swaps when a qualified candidate
    beats the WORST tracked wallet by swap_margin, so ranks near the cut
    line don't oscillate. Wallets absent from `today` keep yesterday's
    state (a fetch failure must not demote anyone).
    """
    res = HysteresisResult()
    wallets = {a: TrackedWallet(**vars(w)) for a, w in existing.items()}

    for addr, (score, is_scalper) in today.items():
        w = wallets.get(addr) or TrackedWallet(address=addr)
        w.score = score
        w.is_scalper = is_scalper
        if score >= cfg.promote_score:
            w.streak_above += 1
            w.streak_below = 0
        elif score < cfg.demote_score:
            w.streak_below += 1
            w.streak_above = 0
        else:
            w.streak_above = 0
            w.streak_below = 0
        wallets[addr] = w

    now = int(time.time())

    # demotions first: frees slots for tonight's promotions
    for w in wallets.values():
        if w.status == "tracked" and w.streak_below >= cfg.demote_streak:
            w.status = "candidate"
            w.demoted_at = now
            w.streak_above = 0
            res.demoted.append(w.address)

    def tracked() -> list[TrackedWallet]:
        return sorted(
            (w for w in wallets.values() if w.status == "tracked"),
            key=lambda w: w.score,
        )

    qualified = sorted(
        (
            w for w in wallets.values()
            if w.status == "candidate"
            and not w.is_scalper
            and w.streak_above >= cfg.promote_streak
        ),
        key=lambda w: w.score, reverse=True,
    )
    for cand in qualified:
        cur = tracked()
        if len(cur) < cfg.max_tracked:
            cand.status = "tracked"
            cand.promoted_at = now
            res.promoted.append(cand.address)
            continue
        worst = cur[0]
        if cand.score > worst.score + cfg.swap_margin:
            worst.status = "candidate"
            worst.demoted_at = now
            res.demoted.append(worst.address)
            cand.status = "tracked"
            cand.promoted_at = now
            res.promoted.append(cand.address)

    res.wallets = wallets
    return res


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


class WalletScout:
    def __init__(
        self,
        *,
        info_client: HyperliquidInfoClient,
        metrics_db: WalletMetricsDB,
        blofin_client,                       # BlofinClient (public reads only)
        config: WalletCopyConfig,
        send_dm: SendDM,
        allowlist: frozenset[int],
    ):
        self._info = info_client
        self._db = metrics_db
        self._blofin = blofin_client
        self._cfg = config
        self._send_dm = send_dm
        self._allowlist = allowlist

    async def run_forever(self) -> None:
        """Nightly loop at cfg.scout_hour_utc. Crash-isolated per run."""
        while True:
            await asyncio.sleep(self._seconds_until_next_run())
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("wallet scout run crashed")

    def _seconds_until_next_run(self) -> float:
        now = datetime.now(timezone.utc)
        target = now.replace(
            hour=self._cfg.scout_hour_utc, minute=0, second=0, microsecond=0,
        )
        secs = (target - now).total_seconds()
        if secs <= 60:   # already past (or within a minute): tomorrow
            secs += 86_400
        return secs

    async def _seed_if_empty(self) -> None:
        """First run: seed the manually vetted addresses as tracked."""
        if await self._db.list_wallets():
            return
        for addr in self._cfg.seed_addresses:
            await self._db.save_tracked_wallet(TrackedWallet(
                address=addr, status="tracked", score=0.0,
                promoted_at=int(time.time()),
            ))
        if self._cfg.seed_addresses:
            logger.info(
                "wallet scout seeded %d tracked wallet(s)",
                len(self._cfg.seed_addresses),
            )

    async def _blofin_bases(self) -> set[str]:
        try:
            instruments = await self._blofin.get_instruments()
        except Exception:
            logger.warning("blofin instrument fetch failed; coverage unknown")
            return set()
        return set(instruments or {})

    async def run_once(self) -> dict:
        """One nightly pass. Returns a summary dict (also used by tests)."""
        await self._seed_if_empty()
        snapshot_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        rows = await self._info.get_leaderboard()
        screened = screen_leaderboard(rows, self._cfg)
        by_addr = {r.address.lower(): r for r in rows}

        # Finalists: top screened + every current tracked/candidate wallet
        # (tracked wallets are ALWAYS re-verified so demotion works).
        known = await self._db.list_wallets()
        finalist_addrs: list[str] = []
        for r in screened[: self._cfg.max_finalists]:
            finalist_addrs.append(r.address)
        for w in known:
            if w.address not in finalist_addrs:
                finalist_addrs.append(w.address)

        blofin_bases = await self._blofin_bases()
        today: dict[str, tuple[float, bool]] = {}
        for addr in finalist_addrs:
            row = by_addr.get(addr.lower())
            if row is None:
                # Not on the leaderboard tonight (or fetch gap): build a
                # minimal row so tracked wallets can still be verified.
                row = LeaderboardRow(address=addr, account_value=0.0)
            try:
                fills = await self._info.get_user_fills(addr)
            except HLInfoError as e:
                logger.warning("userFills failed for %s: %s", short_addr(addr), e)
                continue
            stats = compute_fill_stats(fills, account_value=row.account_value)
            if blofin_bases and stats.coins:
                listed = sum(
                    1 for c in stats.coins
                    if hl_coin_to_blofin_base(c) in blofin_bases
                )
                coverage = listed / len(stats.coins)
            else:
                coverage = 0.0 if blofin_bases else 0.5  # unknown: neutral-ish
            score = copyability_score(
                row, stats, blofin_coverage=coverage, cfg=self._cfg,
            )
            is_scalper = stats.fills_per_day > self._cfg.scalper_fills_per_day
            today[addr] = (score, is_scalper)
            await self._db.upsert_metrics(WalletMetrics(
                address=addr, snapshot_date=snapshot_date,
                account_value=row.account_value,
                day_pnl=row.pnl.get("day", 0.0),
                week_pnl=row.pnl.get("week", 0.0),
                month_pnl=row.pnl.get("month", 0.0),
                alltime_pnl=row.pnl.get("allTime", 0.0),
                month_roi=row.roi.get("month", 0.0),
                volume=row.volume.get("allTime", 0.0),
                fills_per_day=stats.fills_per_day,
                win_rate=stats.win_rate,
                profit_factor=stats.profit_factor,
                max_drawdown_pct=(
                    100.0 * stats.max_drawdown_usd / row.account_value
                    if row.account_value > 0 else 0.0
                ),
                median_hold_min=stats.median_hold_min,
                conviction_median=stats.conviction_median,
                blofin_coverage=coverage,
                top_trade_share=stats.top_trade_share,
                hours_since_fill=stats.hours_since_fill,
                score=score,
            ))
            await asyncio.sleep(_VERIFY_PACE_SEC)

        existing = {w.address: w for w in await self._db.list_wallets()}
        result = apply_hysteresis(existing, today, self._cfg)
        for w in result.wallets.values():
            await self._db.save_tracked_wallet(w)

        # prune stale candidates that scored 0 tonight and hold no streaks
        for w in list(result.wallets.values()):
            if (
                w.status == "candidate" and w.score <= 0
                and w.streak_above == 0 and w.streak_below == 0
                and w.address not in self._cfg.seed_addresses
            ):
                await self._db.remove_wallet(w.address)

        summary = {
            "screened": len(screened),
            "verified": len(today),
            "promoted": result.promoted,
            "demoted": result.demoted,
        }
        if result.promoted or result.demoted:
            digest = self._fmt_digest(result)
            for uid in self._allowlist:
                try:
                    await self._send_dm(uid, digest)
                except Exception:  # noqa: BLE001 - DM failure must not kill the job
                    logger.warning("scout digest DM to %s failed", uid)
        logger.info(
            "wallet scout: screened=%d verified=%d promoted=%d demoted=%d",
            summary["screened"], summary["verified"],
            len(result.promoted), len(result.demoted),
        )
        return summary

    def _fmt_digest(self, result: HysteresisResult) -> str:
        lines = ["Wallet scout update"]
        for a in result.promoted:
            w = result.wallets[a]
            lines.append(f"+ now tracking {short_addr(a)} (score {w.score:g})")
        for a in result.demoted:
            w = result.wallets[a]
            lines.append(f"- stopped tracking {short_addr(a)} (score {w.score:g})")
        tracked = sorted(
            (w for w in result.wallets.values() if w.status == "tracked"),
            key=lambda w: w.score, reverse=True,
        )
        lines.append("")
        lines.append(f"Tracked set ({len(tracked)}):")
        for w in tracked:
            lines.append(f"  {short_addr(w.address)}  score {w.score:g}")
        lines.append("\nNo positions were opened or closed by this change.")
        return "\n".join(lines)
