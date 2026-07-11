"""Nightly archiver: the only source of point-in-time history.

Hyperliquid keeps 5,000 candles per coin/interval (3.5 days of 1m) and no
leaderboard history at all. This job runs once a night (default 03:00 UTC,
an hour after the scout so tracked-set changes have settled) and archives:

  1. the FULL leaderboard JSON (zlib, ~300-600KB/day) + per-address
     account values for screened + known wallets (daily equity anchors),
  2. 1m/15m/1h candles for the relevant coin set (last 48h with a 2h
     overlap so one missed night heals; the in-progress candle is dropped),
  3. hourly funding rates per relevant coin,
  4. known wallets' recent fills into the fills cache,
  5. tracked wallets' raw clearinghouseState (real leverage/margin
     evidence, for calibrating the notional-conviction proxy later),

then prunes by retention policy. Data-only: no DMs, no trades, no
proposals. Paced politely; the whole run costs a rounding error of the
1200 weight/min budget.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from src.config.settings import BacktestConfig, WalletCopyConfig
from src.trading.backtest.data_store import BacktestStore, interval_ms
from src.trading.hl_info_client import (
    HLInfoError,
    HyperliquidInfoClient,
    parse_leaderboard_row,
)
from src.trading.wallet_metrics_db import WalletMetricsDB
from src.trading.wallet_scout import screen_leaderboard, short_addr

logger = logging.getLogger(__name__)

_CANDLE_INTERVALS = ("1m", "15m", "1h")
_MAJORS = ("BTC", "ETH", "SOL")
_LOOKBACK_MS = 48 * 3_600_000       # nightly window
_OVERLAP_MS = 2 * 3_600_000         # heals one missed night
_FILLS_LOOKBACK_MS = 7 * 86_400_000  # coin discovery + cache top-up window


class SnapshotJob:
    def __init__(
        self,
        *,
        info_client: HyperliquidInfoClient,
        store: BacktestStore,
        metrics_db: WalletMetricsDB,
        wallet_cfg: WalletCopyConfig,
        backtest_cfg: BacktestConfig,
        pace_sec: float = 0.7,
    ):
        self._info = info_client
        self._store = store
        self._metrics_db = metrics_db
        self._wallet_cfg = wallet_cfg
        self._cfg = backtest_cfg
        self._pace_sec = pace_sec

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self._seconds_until_next_run())
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("snapshot job run crashed")

    def _seconds_until_next_run(self) -> float:
        now = datetime.now(timezone.utc)
        target = now.replace(
            hour=self._cfg.snapshot_hour_utc, minute=0, second=0, microsecond=0,
        )
        secs = (target - now).total_seconds()
        if secs <= 60:
            secs += 86_400
        return secs

    async def _pace(self) -> None:
        if self._pace_sec:
            await asyncio.sleep(self._pace_sec)

    async def run_once(self) -> dict:
        """One archive pass. Idempotent within a day. Returns a summary."""
        now_ms = int(time.time() * 1000)
        snapshot_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        summary = {
            "date": snapshot_date, "leaderboard_rows": 0, "accounts": 0,
            "coins": 0, "candles": 0, "funding": 0, "fills": 0, "states": 0,
        }

        known = await self._metrics_db.list_wallets()
        known_addrs = {w.address.lower() for w in known}
        tracked_addrs = [
            w.address for w in known if w.status == "tracked"
        ]

        # 1. leaderboard archive + account anchors
        try:
            raw_board = await self._info.get_leaderboard_raw()
        except HLInfoError as e:
            logger.warning("leaderboard archive failed: %s", e)
            raw_board = None
        if raw_board is not None:
            raw_rows = (
                raw_board.get("leaderboardRows")
                if isinstance(raw_board, dict) else raw_board
            ) or []
            parsed = []
            for rr in raw_rows:
                if isinstance(rr, dict):
                    row = parse_leaderboard_row(rr)
                    if row is not None:
                        parsed.append(row)
            await self._store.save_leaderboard_snapshot(
                snapshot_date, raw_board, n_rows=len(parsed),
            )
            summary["leaderboard_rows"] = len(parsed)
            # account anchors: screened universe + everything we track/know
            screened = screen_leaderboard(parsed, self._wallet_cfg)
            accounts: dict[str, float] = {}
            for r in screened:
                accounts[r.address.lower()] = r.account_value
            for r in parsed:
                if r.address.lower() in known_addrs:
                    accounts[r.address.lower()] = r.account_value
            await self._store.upsert_leaderboard_accounts(snapshot_date, accounts)
            summary["accounts"] = len(accounts)
        await self._pace()

        # 2. fills top-up for known wallets (feeds the coin list + the cache)
        coins: set[str] = set(_MAJORS)
        for w in known:
            try:
                fills, complete = await self._info.get_all_user_fills(
                    w.address, start_ms=now_ms - _FILLS_LOOKBACK_MS,
                    end_ms=now_ms, pace_sec=self._pace_sec,
                )
            except HLInfoError as e:
                logger.warning(
                    "fills top-up failed for %s: %s", short_addr(w.address), e,
                )
                continue
            if fills:
                await self._store.upsert_fills(w.address, fills)
                await self._store.add_fills_coverage(
                    w.address, start_ms=now_ms - _FILLS_LOOKBACK_MS,
                    end_ms=now_ms, complete=complete,
                )
                summary["fills"] += len(fills)
                for f in fills:
                    coin = str(f.get("coin", "")).strip()
                    if coin:
                        coins.add(coin)
            await self._pace()

        coin_list = sorted(coins)[: self._cfg.max_snapshot_coins]
        summary["coins"] = len(coin_list)

        # 3. candles: 48h window with overlap, in-progress candle dropped
        for coin in coin_list:
            for interval in _CANDLE_INTERVALS:
                latest = await self._store.latest_candle_ts(coin, interval)
                start = now_ms - _LOOKBACK_MS
                if latest is not None:
                    start = max(start, latest - _OVERLAP_MS)
                try:
                    rows = await self._info.get_candles(
                        coin, interval=interval, start_ms=start, end_ms=now_ms,
                    )
                except HLInfoError as e:
                    logger.warning("candles %s/%s failed: %s", coin, interval, e)
                    continue
                summary["candles"] += await self._store.upsert_candles(
                    coin, interval, rows, drop_open_after_ms=now_ms,
                )
                await self._pace()

        # 4. funding rates per coin
        for coin in coin_list:
            latest = await self._store.latest_funding_ts(coin)
            start = (
                latest + 1 if latest is not None else now_ms - _LOOKBACK_MS
            )
            try:
                rows = await self._info.get_funding_history(
                    coin, start_ms=start, end_ms=now_ms,
                )
            except HLInfoError as e:
                logger.warning("funding %s failed: %s", coin, e)
                continue
            summary["funding"] += await self._store.upsert_funding(coin, rows)
            await self._pace()

        # 5. tracked wallets' raw state (real leverage/margin evidence)
        for addr in tracked_addrs:
            try:
                raw_state = await self._info.get_clearinghouse_state_raw(addr)
            except HLInfoError as e:
                logger.warning("state snapshot failed for %s: %s",
                               short_addr(addr), e)
                continue
            margin = raw_state.get("marginSummary") or {}
            try:
                av = float(margin.get("accountValue", 0.0))
            except (TypeError, ValueError):
                av = 0.0
            await self._store.save_wallet_state(
                addr, snapshot_date, account_value=av, raw_body=raw_state,
            )
            summary["states"] += 1
            await self._pace()

        # 6. retention
        await self._store.prune(
            now_ms=now_ms,
            candle_1m_keep_days=self._cfg.candle_1m_keep_days,
            candle_15m_keep_days=self._cfg.candle_15m_keep_days,
            fills_keep_days=self._cfg.fills_keep_days,
        )

        logger.info(
            "snapshot: board=%d accounts=%d coins=%d candles=%d "
            "funding=%d fills=%d states=%d",
            summary["leaderboard_rows"], summary["accounts"], summary["coins"],
            summary["candles"], summary["funding"], summary["fills"],
            summary["states"],
        )
        return summary


def candle_gap_check(latest_ts: int | None, now_ms: int, interval: str) -> bool:
    """True when the archive has an unhealable gap for this interval (more
    than the upstream 5000-candle retention has elapsed since last store)."""
    if latest_ts is None:
        return False
    return (now_ms - latest_ts) > 5000 * interval_ms(interval)
