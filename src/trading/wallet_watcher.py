"""Wallet watcher: mirror tracked Hyperliquid wallets, confirm-gated.

Polls each tracked wallet's clearinghouseState every poll_sec and diffs
against the last stored snapshot to detect deltas: open, add, reduce,
close, flip. For copyable NEW opens it derives our own protective levels
(the wallet's stops are invisible, so stop = entry -/+ atr_mult * ATR and
TPs at 1R/2R/3R) and routes a proposal through engine.propose_copy: the
allowlisted user gets a DM and must /autotrade copy confirm. NOTHING here
places an order directly.

Gates before proposing an open:
  - wallet not flagged as a scalper by the scout (intraday noise)
  - conviction floor: the wallet's margin-in-trade / account value
  - coin listed on Blofin (resolve_inst_id)
  - per (wallet, coin) proposal cooldown
  - first poll after boot only baselines (never fires on restart)

Reduces/closes/flips DM immediately so exits can be mirrored by hand.
Auto-reducing is intentionally NOT implemented; the mirror_exits flag is
reserved so a future opt-in cannot exist by accident.

Sizing: the proposal carries size_pct_override = the wallet's
margin/equity fraction, so we risk the same fraction of Luke's balance as
they risked of theirs, always capped by his own size prefs in the engine.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Awaitable, Callable

from src.config.settings import WalletCopyConfig
from src.trading.hl_info_client import (
    HLInfoError,
    HyperliquidInfoClient,
    WalletPosition,
    WalletState,
)
from src.trading.wallet_metrics_db import StoredPosition, WalletMetricsDB
from src.trading.wallet_scout import hl_coin_to_blofin_base, short_addr

logger = logging.getLogger(__name__)

SendDM = Callable[[int, str], Awaitable[None]]

# Adds smaller than this fraction of the existing position are noise.
_MIN_ADD_FRACTION = 0.25
# Reduces smaller than this fraction of the position are noise (dust exits).
_MIN_REDUCE_FRACTION = 0.10


@dataclass(frozen=True)
class PositionDelta:
    kind: str          # 'open' | 'add' | 'reduce' | 'close' | 'flip'
    coin: str
    prev_szi: float
    curr_szi: float
    entry_px: float = 0.0
    leverage: float = 0.0
    notional: float = 0.0
    margin_used: float = 0.0


def diff_positions(
    prev: dict[str, StoredPosition], curr: dict[str, WalletPosition],
) -> list[PositionDelta]:
    """Pure position diff between the stored and live snapshots."""
    deltas: list[PositionDelta] = []
    for coin, pos in curr.items():
        old = prev.get(coin)
        if old is None or old.szi == 0.0:
            deltas.append(PositionDelta(
                kind="open", coin=coin, prev_szi=0.0, curr_szi=pos.szi,
                entry_px=pos.entry_px, leverage=pos.leverage,
                notional=pos.notional, margin_used=pos.margin_used,
            ))
            continue
        if (old.szi > 0) != (pos.szi > 0):
            deltas.append(PositionDelta(
                kind="flip", coin=coin, prev_szi=old.szi, curr_szi=pos.szi,
                entry_px=pos.entry_px, leverage=pos.leverage,
                notional=pos.notional, margin_used=pos.margin_used,
            ))
        elif abs(pos.szi) > abs(old.szi):
            deltas.append(PositionDelta(
                kind="add", coin=coin, prev_szi=old.szi, curr_szi=pos.szi,
                entry_px=pos.entry_px, leverage=pos.leverage,
                notional=pos.notional, margin_used=pos.margin_used,
            ))
        elif abs(pos.szi) < abs(old.szi):
            deltas.append(PositionDelta(
                kind="reduce", coin=coin, prev_szi=old.szi, curr_szi=pos.szi,
                entry_px=pos.entry_px, leverage=pos.leverage,
                notional=pos.notional, margin_used=pos.margin_used,
            ))
    for coin, old in prev.items():
        if old.szi != 0.0 and coin not in curr:
            deltas.append(PositionDelta(
                kind="close", coin=coin, prev_szi=old.szi, curr_szi=0.0,
            ))
    return deltas


def compute_atr(candles: list[dict], period: int = 14) -> float | None:
    """Classic ATR over Hyperliquid candleSnapshot rows (t/o/h/l/c strings).

    Returns None when there aren't enough candles to be meaningful.
    """
    rows = []
    for c in candles:
        try:
            rows.append((float(c["h"]), float(c["l"]), float(c["c"])))
        except (KeyError, TypeError, ValueError):
            continue
    if len(rows) < period + 1:
        return None
    trs: list[float] = []
    prev_close = rows[0][2]
    for high, low, close in rows[1:]:
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close
    window = trs[-period:]
    atr = sum(window) / len(window)
    return atr if atr > 0 else None


def derive_protective_levels(
    *, entry: float, is_long: bool, atr: float, atr_mult: float,
) -> tuple[float, list[float]]:
    """Volatility-derived stop + 1R/2R/3R take-profit ladder.

    The tracked wallet's real stop is invisible on-chain, so risk one
    ATR-multiple and ladder TPs at 1, 2 and 3 times that risk.
    """
    risk = atr * atr_mult
    if is_long:
        stop = entry - risk
        tps = [entry + risk, entry + 2 * risk, entry + 3 * risk]
    else:
        stop = entry + risk
        tps = [entry - risk, entry - 2 * risk, entry - 3 * risk]
    return stop, [t for t in tps if t > 0]


class WalletWatcher:
    def __init__(
        self,
        *,
        info_client: HyperliquidInfoClient,
        metrics_db: WalletMetricsDB,
        engine,                              # AutotradeEngine
        blofin_client,                       # BlofinClient (public reads only)
        config: WalletCopyConfig,
        send_dm: SendDM,
        allowlist: frozenset[int],
        venue=None,                          # trading venue; reconciler only
    ):
        self._info = info_client
        self._db = metrics_db
        self._engine = engine
        self._blofin = blofin_client
        self._cfg = config
        self._send_dm = send_dm
        self._allowlist = allowlist
        self._venue = venue
        self._polls_since_reconcile = 0
        # (address, coin) -> monotonic seconds of the last proposal
        self._last_proposal: dict[tuple[str, str], float] = {}
        # addresses baselined since THIS process started; a fresh boot
        # re-baselines even if the db row exists, so downtime deltas are
        # reported as a summary rather than fired one by one.
        self._session_baselined: set[str] = set()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("wallet watcher poll crashed")
            await asyncio.sleep(self._cfg.poll_sec)

    async def poll_once(self) -> None:
        tracked = await self._db.list_wallets(status="tracked")
        for w in tracked:
            try:
                await self._poll_wallet(w.address)
            except HLInfoError as e:
                logger.warning("watcher fetch failed for %s: %s",
                               short_addr(w.address), e)
            except Exception:
                logger.exception("watcher crashed on %s", short_addr(w.address))
        # reconcile linked copy positions every ~20 polls (5 min at 15s)
        self._polls_since_reconcile += 1
        if self._polls_since_reconcile >= 20:
            self._polls_since_reconcile = 0
            try:
                await self._reconcile_copy_trades(tracked)
            except Exception:  # noqa: BLE001 - bookkeeping never kills polling
                logger.exception("copy-trade reconciler crashed")

    async def _poll_wallet(self, address: str) -> None:
        state = await self._info.get_clearinghouse_state(address)
        stored = {
            coin: pos for coin, pos in (await self._db.get_positions(address)).items()
        }
        first_this_session = address not in self._session_baselined
        had_baseline = await self._db.is_baselined(address)

        if first_this_session:
            self._session_baselined.add(address)
            if not had_baseline:
                # brand-new wallet: silently baseline, never fire history
                await self._save(address, state)
                await self._db.log_event(address, "*", "baseline",
                                         {"positions": len(state.positions)})
                return
            # restart with an existing baseline: report what changed while
            # we were down, but never propose from stale deltas.
            deltas = diff_positions(stored, state.positions)
            if deltas:
                await self._save(address, state)
                await self._notify_all(
                    f"Watcher restarted. {short_addr(address)} changed while "
                    f"offline: "
                    + ", ".join(f"{d.kind} {d.coin}" for d in deltas)
                    + ". No proposals from stale deltas."
                )
                for d in deltas:
                    await self._db.log_event(address, d.coin, f"stale_{d.kind}")
            return

        deltas = diff_positions(stored, state.positions)
        if not deltas:
            return
        await self._save(address, state)
        for d in deltas:
            await self._handle_delta(address, state, d)

    async def _save(self, address: str, state: WalletState) -> None:
        await self._db.replace_positions(address, {
            coin: StoredPosition(
                coin=coin, szi=p.szi, entry_px=p.entry_px,
                leverage=p.leverage, notional=p.notional,
            )
            for coin, p in state.positions.items()
        })

    async def _handle_delta(
        self, address: str, state: WalletState, d: PositionDelta,
    ) -> None:
        await self._db.log_event(address, d.coin, d.kind, {
            "prev": d.prev_szi, "curr": d.curr_szi, "notional": d.notional,
        })
        if d.kind in ("open", "flip"):
            await self._maybe_propose_open(address, state, d)
        elif d.kind == "add":
            frac = (
                (abs(d.curr_szi) - abs(d.prev_szi)) / abs(d.prev_szi)
                if d.prev_szi else 1.0
            )
            if frac >= _MIN_ADD_FRACTION:
                await self._notify_all(
                    f"{short_addr(address)} added {frac * 100:.0f}% to their "
                    f"{d.coin} {'long' if d.curr_szi > 0 else 'short'} "
                    f"(now ~${d.notional:,.0f}). FYI only, no proposal."
                )
        elif d.kind in ("reduce", "close"):
            await self._notify_exit(address, d)

    async def _maybe_propose_open(
        self, address: str, state: WalletState, d: PositionDelta,
    ) -> None:
        label = short_addr(address)
        is_long = d.curr_szi > 0
        side = "LONG" if is_long else "SHORT"

        # flip: always tell the user the old side is gone
        if d.kind == "flip":
            await self._notify_all(
                f"{label} FLIPPED {d.coin} to {side}. If you copied the old "
                f"side, it is now against the wallet you follow."
            )

        # Every gate skip is shadow-logged: without skip records we cannot
        # tell later whether a wallet's edge died or our guards ate it.

        # gate: scalper wallets' intraday churn is not copyable
        tw = await self._db.get_tracked_wallet(address)
        if tw is not None and tw.is_scalper:
            await self._db.log_event(address, d.coin, "skip_scalper", {
                "side": side, "notional": d.notional,
            })
            return

        # gate: conviction floor (their margin in this trade vs their equity)
        conviction = (
            d.margin_used / state.account_value
            if state.account_value > 0 and d.margin_used > 0 else 0.0
        )
        if conviction < self._cfg.conviction_floor:
            await self._db.log_event(address, d.coin, "skip_conviction", {
                "side": side, "conviction": round(conviction, 4),
                "floor": self._cfg.conviction_floor, "px": d.entry_px,
            })
            return

        # gate: listed on Blofin
        base = hl_coin_to_blofin_base(d.coin)
        try:
            info = await self._blofin.resolve_inst_id(base)
        except Exception:
            logger.warning("blofin resolve failed for %s", base)
            return
        if info is None:
            await self._db.log_event(address, d.coin, "skip_unlisted", {
                "side": side, "notional": d.notional,
            })
            await self._notify_all(
                f"{label} opened {d.coin} {side} (~${d.notional:,.0f}, "
                f"{conviction * 100:.0f}% of their equity) but {base} is not "
                f"listed on Blofin. Not copyable."
            )
            return

        # gate: cooldown per (wallet, coin)
        key = (address, d.coin)
        now = time.monotonic()
        last = self._last_proposal.get(key)
        if last is not None and now - last < self._cfg.proposal_cooldown_min * 60:
            await self._db.log_event(address, d.coin, "skip_cooldown", {
                "side": side, "px": d.entry_px,
            })
            return
        self._last_proposal[key] = now

        # derive our own protective levels from volatility
        entry = d.entry_px
        stop, tps = None, []
        end_ms = int(time.time() * 1000)
        lookback_ms = (self._cfg.atr_period + 10) * _interval_ms(self._cfg.atr_interval)
        try:
            candles = await self._info.get_candles(
                d.coin, interval=self._cfg.atr_interval,
                start_ms=end_ms - lookback_ms, end_ms=end_ms,
            )
            atr = compute_atr(candles, self._cfg.atr_period)
        except HLInfoError:
            atr = None
        if atr is not None and entry > 0:
            stop, tps = derive_protective_levels(
                entry=entry, is_long=is_long, atr=atr,
                atr_mult=self._cfg.atr_mult,
            )

        size_pct = round(conviction * 100.0, 2)
        leverage = int(d.leverage) if d.leverage and d.leverage > 0 else 0
        note_lines = [
            f"Their size: ~${d.notional:,.0f} "
            f"({conviction * 100:.1f}% of their ${state.account_value:,.0f} equity).",
            "SL/TPs are OURS, derived from recent volatility (ATR); the "
            "wallet's real exits are invisible.",
        ]
        if stop is None:
            note_lines.append(
                "Could not compute ATR levels; NO stop attached. Set one "
                "manually if you confirm."
            )
        sig = SimpleNamespace(
            pair=f"{base}/USDT",
            side=side,
            leverage=leverage,
            entry=entry if entry > 0 else None,
            take_profits=tps,
            stop_loss=stop,
            size_pct_override=size_pct,
            note="\n".join(note_lines),
        )
        # meta arms the engine's confirm-time deviation gate and writes the
        # attribution row that links a filled copy back to this leader.
        proposal_id = await self._engine.propose_copy(
            sig, source=f"wallet {label}",
            meta={
                "leader_address": address,
                "coin": d.coin,
                "inst_id": getattr(info, "inst_id", ""),
                "proposal_price": entry if entry > 0 else None,
                "atr": atr,
                "max_deviation_atr": self._cfg.copy_max_deviation_atr,
            },
        )
        await self._db.log_event(address, d.coin, "proposed", {
            "side": side, "size_pct": size_pct, "stop": stop, "tps": tps,
            "proposal_id": proposal_id,
        })

    async def _linked_note(self, address: str, coin: str) -> str:
        """One line naming any of OUR open positions linked to this leader's
        coin, so an exit DM lands with full context."""
        try:
            links = await self._db.open_copy_trades(address, coin)
        except Exception:  # noqa: BLE001
            return ""
        if not links:
            return ""
        refs = ", ".join(
            f"order {t.order_ref or '?'} ({t.size_base:g} {coin})"
            if t.size_base else f"order {t.order_ref or '?'}"
            for t in links
        )
        return f"\nYou hold a linked copy: {refs}."

    async def _notify_exit(self, address: str, d: PositionDelta) -> None:
        label = short_addr(address)
        side = "long" if d.prev_szi > 0 else "short"
        linked = await self._linked_note(address, d.coin)
        if d.kind == "close":
            await self._notify_all(
                f"{label} CLOSED their {d.coin} {side}. If you copied it, "
                f"consider closing yours (confirm-gated; nothing was done "
                f"automatically).{linked}"
            )
            return
        frac = (
            (abs(d.prev_szi) - abs(d.curr_szi)) / abs(d.prev_szi)
            if d.prev_szi else 0.0
        )
        if frac < _MIN_REDUCE_FRACTION:
            return
        await self._notify_all(
            f"{label} reduced their {d.coin} {side} by {frac * 100:.0f}%. "
            f"If you copied it, consider scaling out (nothing was done "
            f"automatically).{linked}"
        )

    async def _reconcile_copy_trades(self, tracked) -> None:
        """Mark linked copies whose venue position vanished without a
        recorded close as closed_unreconciled (manual close). Their pnl is
        unknown and they are EXCLUDED from per-leader stop sums rather than
        guessed."""
        if self._venue is None:
            return
        for w in tracked:
            links = await self._db.open_copy_trades(w.address)
            for t in links:
                pair = f"{hl_coin_to_blofin_base(t.coin)}/USDT"
                try:
                    pos = await self._venue.get_open_position(
                        t.telegram_user_id, pair,
                    )
                except Exception:  # noqa: BLE001 - unreadable = try next cycle
                    continue
                if pos == 0:
                    await self._db.close_copy_trade(
                        t.id, close_reason="manual",
                        realized_pnl=None, status="closed_unreconciled",
                    )
                    await self._db.log_event(
                        w.address, t.coin, "copy_unreconciled",
                        {"order_ref": t.order_ref},
                    )
                    await self._notify_all(
                        f"Linked {t.coin} copy (order {t.order_ref or '?'}) "
                        f"is no longer open on the exchange and no close was "
                        f"recorded here; marked closed_unreconciled. Its pnl "
                        f"is unknown and won't count toward the leader's "
                        f"running total."
                    )

    async def _notify_all(self, text: str) -> None:
        for uid in self._allowlist:
            try:
                await self._send_dm(uid, text)
            except Exception:  # noqa: BLE001
                logger.warning("watcher DM to %s failed", uid)


def _interval_ms(interval: str) -> int:
    table = {
        "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
        "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
    }
    return table.get(interval, 3_600_000)
