"""Cache-first data feed for backtest runs.

Reads from the BacktestStore archive and tops up from the live API where
the archive is thin. The upstream API only serves the most recent 5000
candles per interval, so old gaps are unfixable: the feed splices 15m
candles into 1m gaps and reports the spliced spans so the simulator can
honesty-flag trades entered there.
"""

from __future__ import annotations

import logging
import time

from src.trading.backtest.data_store import BacktestStore, Candle, interval_ms
from src.trading.backtest.position_events import AccountValueCurve
from src.trading.backtest.simulator import MarketData
from src.trading.hl_info_client import HLInfoError, HyperliquidInfoClient

logger = logging.getLogger(__name__)

# When native-1m coverage over a span is below this, splice 15m candles in.
_SPLICE_THRESHOLD = 0.5
_DAY_MS = 86_400_000


def _date_to_ms(snapshot_date: str) -> int:
    from datetime import datetime, timezone

    dt = datetime.strptime(snapshot_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def splice_candles(
    fine: list[Candle], coarse: list[Candle], *, coarse_step_ms: int,
) -> tuple[list[Candle], list[tuple[int, int]]]:
    """Native fine candles plus coarse candles ONLY where fine is absent.

    Returns (merged sorted series, coarse spans [start, end) actually
    used). A coarse candle is used only when no fine candle opens inside
    its window, so the merged series never double-counts a minute.
    """
    fine_ts = {c.ts for c in fine}
    merged = list(fine)
    spans: list[tuple[int, int]] = []
    for c in coarse:
        window = range(c.ts, c.ts + coarse_step_ms, 60_000)
        if any(ts in fine_ts for ts in window):
            continue
        merged.append(c)
        if spans and spans[-1][1] == c.ts:
            spans[-1] = (spans[-1][0], c.ts + coarse_step_ms)
        else:
            spans.append((c.ts, c.ts + coarse_step_ms))
    merged.sort(key=lambda c: c.ts)
    return merged, spans


class BacktestDataFeed:
    def __init__(
        self, *, info_client: HyperliquidInfoClient, store: BacktestStore,
        pace_sec: float = 0.3,
    ):
        self._info = info_client
        self._store = store
        self._pace_sec = pace_sec

    # ---- fills -------------------------------------------------------------

    async def fills(
        self, address: str, *, start_ms: int, end_ms: int,
    ) -> tuple[list[dict], bool]:
        """Cached fills when a complete covering window exists, else a live
        fetch that also tops up the cache."""
        for c_start, c_end, complete in await self._store.fills_coverage(address):
            if complete and c_start <= start_ms and c_end >= end_ms:
                return (
                    await self._store.get_fills(
                        address, start_ms=start_ms, end_ms=end_ms,
                    ),
                    True,
                )
        try:
            fills, complete = await self._info.get_all_user_fills(
                address, start_ms=start_ms, end_ms=end_ms,
                pace_sec=self._pace_sec,
            )
        except HLInfoError as e:
            logger.warning("live fills fetch failed for %s: %s", address, e)
            cached = await self._store.get_fills(
                address, start_ms=start_ms, end_ms=end_ms,
            )
            return cached, False
        if fills:
            await self._store.upsert_fills(address, fills)
        await self._store.add_fills_coverage(
            address, start_ms=start_ms, end_ms=end_ms, complete=complete,
        )
        return fills, complete

    # ---- candles ------------------------------------------------------------

    async def candles(
        self, coin: str, interval: str, *, start_ms: int, end_ms: int,
    ) -> list[Candle]:
        """Archive candles, topped up from the API when coverage is thin.
        The API can only help within its 5000-candle retention window."""
        coverage = await self._store.candle_coverage(
            coin, interval, start_ms=start_ms, end_ms=end_ms,
        )
        if coverage < 0.95:
            step = interval_ms(interval)
            api_floor = int(time.time() * 1000) - 5000 * step
            fetch_start = max(start_ms, api_floor)
            if fetch_start < end_ms:
                try:
                    rows = await self._info.get_candles(
                        coin, interval=interval,
                        start_ms=fetch_start, end_ms=end_ms,
                    )
                    await self._store.upsert_candles(
                        coin, interval, rows,
                        drop_open_after_ms=int(time.time() * 1000),
                    )
                except HLInfoError as e:
                    logger.warning("candle top-up %s/%s failed: %s",
                                   coin, interval, e)
        return await self._store.get_candles(
            coin, interval, start_ms=start_ms, end_ms=end_ms,
        )

    # ---- funding -------------------------------------------------------------

    async def funding(
        self, coin: str, *, start_ms: int, end_ms: int,
    ) -> list[tuple[int, float]]:
        have = await self._store.get_funding(
            coin, start_ms=start_ms, end_ms=end_ms,
        )
        expected = max(1, (end_ms - start_ms) // 3_600_000)
        if len(have) < expected * 0.9:
            try:
                rows = await self._info.get_funding_history(
                    coin, start_ms=start_ms, end_ms=end_ms,
                )
                await self._store.upsert_funding(coin, rows)
                have = await self._store.get_funding(
                    coin, start_ms=start_ms, end_ms=end_ms,
                )
            except HLInfoError as e:
                logger.warning("funding top-up %s failed: %s", coin, e)
        return have

    # ---- account value curve ---------------------------------------------------

    async def account_value_curve(
        self, address: str, fills: list[dict],
    ) -> AccountValueCurve:
        """Anchors from archived daily snapshots plus a live now-anchor."""
        anchors = [
            (_date_to_ms(date) , av)
            for date, av in await self._store.account_values(address)
        ]
        fallback = 0.0
        try:
            state = await self._info.get_clearinghouse_state(address)
            if state.account_value > 0:
                anchors.append((int(time.time() * 1000), state.account_value))
                fallback = state.account_value
        except HLInfoError as e:
            logger.warning("live account value failed for %s: %s", address, e)
        return AccountValueCurve(anchors=anchors, fills=fills, fallback=fallback)

    # ---- assembled market data ---------------------------------------------------

    async def market_data(
        self, coins: set[str], *, start_ms: int, end_ms: int,
    ) -> MarketData:
        market = MarketData()
        atr_lookback = 45 * 3_600_000
        for coin in sorted(coins):
            fine = await self.candles(
                coin, "1m", start_ms=start_ms, end_ms=end_ms,
            )
            coarse = await self.candles(
                coin, "15m", start_ms=start_ms, end_ms=end_ms,
            )
            span = max(1, end_ms - start_ms)
            fine_cov = len(fine) * 60_000 / span
            if fine_cov < _SPLICE_THRESHOLD and coarse:
                merged, spans = splice_candles(
                    fine, coarse, coarse_step_ms=interval_ms("15m"),
                )
                market.candles_1m[coin] = merged
                market.coarse_ranges[coin] = spans
            else:
                market.candles_1m[coin] = fine
            market.candles_1h[coin] = await self.candles(
                coin, "1h", start_ms=start_ms - atr_lookback, end_ms=end_ms,
            )
            market.funding[coin] = await self.funding(
                coin, start_ms=start_ms, end_ms=end_ms,
            )
        return market
