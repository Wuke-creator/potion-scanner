"""Signal-driven autotrade engine (venue-agnostic).

When the router records a new "Perp Bot Calls" signal it calls
``on_new_signal``. For each allowlisted, connected, opted-in user the engine:

  1. checks eligibility (allowlist AND Elite-active AND venue-connected
     AND /autotrade on + disclosure accepted),
  2. atomically claims (user, signal) so a re-broadcast can't double-fire,
  3. sizes collateral as ``balance * size_pct`` (clamped),
  4. asks the venue to build a plan (symbol resolution, price, sizing,
     leverage capped by the signal, the user max, and the asset max),
  5. in dry-run: DMs the intended trade and places nothing,
     live: consumes a daily-cap slot, places the trade, DMs the result.

The engine is venue-agnostic: it talks to a ``Venue`` (Hyperliquid or Blofin)
and never sees an address, agent key, or API secret. Every user is isolated:
one user's failure never blocks another. Nothing fires unless
``config.enabled`` and the allowlist is non-empty; the router only calls this
for the configured source channel.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from src.config.settings import AutotradeConfig
from src.trading.autotrade_risk import AutotradeRiskGuard
from src.trading.venue import Venue, VenueError, VenuePlan

logger = logging.getLogger(__name__)

SendDM = Callable[[int, str], Awaitable[None]]

_MANAGE_URL = {
    "hyperliquid": "app.hyperliquid.xyz",
    "blofin": "blofin.com",
}

# How long a parsed cabal call stays confirmable. Discretionary swing calls
# are actionable for a while; stale ones must not fire hours later.
_COPY_TTL_SEC = 15 * 60


def _fmt_num(value: float) -> str:
    return f"{value:g}"


class AutotradeEngine:
    def __init__(
        self,
        *,
        config: AutotradeConfig,
        max_collateral_usdc: float,
        venue: Venue,
        prefs_db,
        verification_db,
        send_dm: SendDM,
        copy_store=None,   # WalletMetricsDB; attribution for wallet copies
    ):
        self._config = config
        self._max_collateral_usdc = max_collateral_usdc
        self._venue = venue
        self._prefs_db = prefs_db
        self._verification_db = verification_db
        self._send_dm = send_dm
        self._copy_store = copy_store
        # The risk guard only runs when explicitly enabled AND the venue can
        # supply an account snapshot. Blofin can't yet, so it's off there.
        self._guard_on = bool(
            getattr(config, "risk_enabled", True)
            and getattr(venue, "supports_risk_guard", False)
        )
        self._risk = AutotradeRiskGuard(config=config, venue=venue, prefs_db=prefs_db)
        # uid -> (synthetic signal, expires_at, meta). Confirm-gated copy
        # proposals from discretionary channels (cabal) and the wallet
        # watcher. In-memory by design: a restart drops pending proposals
        # rather than firing stale ones. meta (wallet copies only) carries
        # leader/price/ATR context for the confirm-time deviation gate and
        # attribution; cabal proposals have meta=None and skip both.
        self._pending_copies: dict[int, tuple[object, float, dict | None]] = {}

    def attach_copy_store(self, copy_store) -> None:
        """Late-bind the attribution store (built after the engine)."""
        self._copy_store = copy_store

    async def on_new_signal(self, signal) -> None:
        """Entry point from the router. Never raises into the caller."""
        if not self._config.enabled or not self._config.allowlist:
            return
        side = (getattr(signal, "side", None) or "").strip().upper()
        if side not in ("LONG", "SHORT"):
            return  # no clear direction -> nothing to trade
        signal_id = getattr(signal, "id", None)
        if signal_id is None:
            return
        for uid in self._config.allowlist:
            try:
                await self._maybe_fire(uid, signal, int(signal_id), side)
            except Exception:  # noqa: BLE001 - isolate per user
                logger.exception(
                    "autotrade crashed for user=%s signal=%s", uid, signal_id,
                )

    async def manual_fire(
        self, uid: int, *, pair: str, side: str, leverage: int = 0,
        stop_loss: float | None = None, take_profit: float | None = None,
    ) -> None:
        """User-initiated single trade (the /autotrade fire command).

        Runs the exact live placement path _maybe_fire uses, for the one
        invoking user, off a synthetic signal. Honors every gate + dry_run
        + the risk guard. Never raises into the caller.
        """
        import time
        from types import SimpleNamespace

        side = (side or "").strip().upper()
        if side not in ("LONG", "SHORT"):
            await self._dm(uid, "Manual fire needs a direction: long or short.")
            return
        if uid not in self._config.allowlist:
            return
        sid = int(time.time() * 1000)  # unique id so it always claims + never dedupes
        sig = SimpleNamespace(
            id=sid, pair=pair, side=side, leverage=leverage or 0,
            tp1=take_profit, stop_loss=stop_loss,
        )
        try:
            await self._maybe_fire(uid, sig, sid, side)
        except Exception:  # noqa: BLE001
            logger.exception("manual_fire crashed for user=%s", uid)
            await self._dm(uid, "Manual fire hit an unexpected error.")

    async def propose_copy(
        self, sig, *, source: str = "cabal", meta: dict | None = None,
    ) -> int | None:
        """Confirm-gated copy for discretionary calls and wallet copies.

        Human-typed calls parse tolerantly, so nothing fires by itself: each
        eligible user gets a DM preview and must reply /autotrade copy
        confirm within the TTL. Never raises into the router.

        Wallet-watcher proposals pass ``meta`` (leader_address, coin,
        inst_id, proposal_price, atr, max_deviation_atr): it arms the
        confirm-time price-deviation gate and writes attribution rows.
        Returns the proposal id when at least one user was DM'd.
        """
        import time

        if not self._config.enabled or not self._config.allowlist:
            return None
        side = (getattr(sig, "side", "") or "").strip().upper()
        if side not in ("LONG", "SHORT"):
            return None
        leverage = getattr(sig, "leverage", None) or self._config.copy_default_leverage
        tps = list(getattr(sig, "take_profits", None) or [])
        from types import SimpleNamespace

        synthetic = SimpleNamespace(
            id=int(time.time() * 1000),
            pair=getattr(sig, "pair", ""),
            side=side,
            leverage=int(leverage),
            tp1=tps[0] if len(tps) > 0 else None,
            tp2=tps[1] if len(tps) > 1 else None,
            tp3=tps[2] if len(tps) > 2 else None,
            stop_loss=getattr(sig, "stop_loss", None),
            # optional per-proposal sizing (wallet copies mirror the tracked
            # wallet's equity fraction); the fire path caps it at the user's
            # own size prefs, so it can only ever shrink a trade.
            size_pct_override=getattr(sig, "size_pct_override", None),
            # ATR risk distance for wallet copies (None for channel calls, whose
            # levels come from the caller and must pass through untouched). Its
            # presence is what tells the fire path to re-anchor.
            copy_risk_per_unit=getattr(sig, "risk_per_unit", None),
        )
        # Channel-call auto-fire: when enabled, cabal entries place straight
        # away (the fire path DMs the fill or failure). Wallet proposals
        # (meta present) are NEVER auto-fired: their price-deviation gate is
        # designed to run at confirm time, so skipping confirm would skip it.
        if self._config.copy_auto_fire and meta is None:
            fired_any = False
            for uid in self._config.allowlist:
                try:
                    prefs = await self._copy_eligible(uid)
                    if prefs is None:
                        continue
                    fired_any = True
                    await self._dm(
                        uid,
                        f"Auto-copying {source} call: {synthetic.pair} {side} "
                        f"{synthetic.leverage}x...",
                    )
                    await self._maybe_fire(
                        uid, synthetic, int(synthetic.id), side,
                    )
                except Exception:  # noqa: BLE001 - isolate per user
                    logger.exception("auto copy failed for user=%s", uid)
            return int(synthetic.id) if fired_any else None

        proposed_any = False
        for uid in self._config.allowlist:
            try:
                prefs = await self._copy_eligible(uid)
                if prefs is None:
                    continue
                self._pending_copies[uid] = (
                    synthetic, time.time() + _COPY_TTL_SEC, meta,
                )
                if meta is not None and self._copy_store is not None:
                    await self._copy_store.insert_copy_trade(
                        leader_address=str(meta.get("leader_address", "")),
                        coin=str(meta.get("coin", "")),
                        inst_id=str(meta.get("inst_id", "")),
                        telegram_user_id=uid,
                        side=side,
                        proposal_id=int(synthetic.id),
                        proposal_price=meta.get("proposal_price"),
                        atr_at_proposal=meta.get("atr"),
                        stop_price=getattr(synthetic, "stop_loss", None),
                    )
                proposed_any = True
                override = getattr(sig, "size_pct_override", None)
                shown_pct = (
                    min(float(override), prefs.size_pct)
                    if override else prefs.size_pct
                )
                await self._dm(
                    uid, self._fmt_copy_preview(sig, leverage, source, shown_pct),
                )
            except Exception:  # noqa: BLE001 - isolate per user
                logger.exception("propose_copy failed for user=%s", uid)
        return int(synthetic.id) if proposed_any else None

    async def confirm_copy(self, uid: int) -> bool:
        """Fire the pending copy proposal for this user. True if placed.

        Wallet-copy proposals (meta present) pass a price-deviation gate
        first: with 0-15 minutes between proposal and confirm, an entry
        that already ran k x ATR against the proposal price is adverse
        selection, not a fill. Cancel beats chase. A price that cannot be
        READ keeps the proposal pending (retryable) but never fires: the
        gate fails closed on unreadable state, matching the risk guard.
        """
        import time

        pend = self._pending_copies.get(uid)
        if pend is None:
            await self._dm(uid, "No pending call to copy (or it expired).")
            return False
        synthetic, expires, meta = pend
        if time.time() > expires:
            self._pending_copies.pop(uid, None)
            await self._mark_copy(uid, synthetic, "expired")
            await self._dm(uid, "That call expired (15 min limit). Not placed.")
            return False

        if meta is not None:
            gate = await self._deviation_gate(uid, synthetic, meta)
            if gate == "cancel":
                self._pending_copies.pop(uid, None)
                return False
            if gate == "retry":
                return False   # pending kept; user can confirm again

        self._pending_copies.pop(uid, None)
        try:
            result = await self._maybe_fire(
                uid, synthetic, int(synthetic.id), synthetic.side,
            )
            if result is not None and meta is not None:
                await self._mark_copy_filled(uid, synthetic, result)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("confirm_copy crashed for user=%s", uid)
            await self._dm(uid, "Copy hit an unexpected error.")
            return False

    async def _deviation_gate(self, uid: int, synthetic, meta: dict) -> str:
        """'pass' | 'cancel' | 'retry' (price unreadable, kept pending)."""
        proposal_price = meta.get("proposal_price")
        atr = meta.get("atr")
        max_dev = float(meta.get("max_deviation_atr") or 0.0)
        if not proposal_price or not atr or max_dev <= 0:
            return "pass"   # nothing to gate against (no ATR at proposal)
        try:
            current = await self._venue.get_price(synthetic.pair)
        except Exception:  # noqa: BLE001 - unreadable price = do not fire
            logger.warning("deviation gate could not read price for %s",
                           synthetic.pair)
            await self._dm(
                uid,
                "Could not verify the current price; NOT placed. The "
                "proposal is still pending, try /autotrade copy confirm "
                "again in a moment.",
            )
            return "retry"
        is_long = synthetic.side == "LONG"
        adverse = (
            current - float(proposal_price) if is_long
            else float(proposal_price) - current
        )
        moved_atr = adverse / float(atr) if atr else 0.0
        if moved_atr > max_dev:
            await self._mark_copy(uid, synthetic, "cancelled_deviation")
            await self._dm(
                uid,
                f"Auto-cancelled: {synthetic.pair} moved "
                f"{moved_atr:.2f}x ATR against the entry since the proposal "
                f"(limit {max_dev:g}x). Chasing an entry that already ran "
                f"is how copy latency turns into losses.",
            )
            logger.info(
                "deviation gate cancelled %s for user=%s (%.2fx ATR)",
                synthetic.pair, uid, moved_atr,
            )
            return "cancel"
        return "pass"

    async def _mark_copy(self, uid: int, synthetic, status: str) -> None:
        if self._copy_store is None:
            return
        try:
            await self._copy_store.set_copy_trade_status(
                int(synthetic.id), uid, status,
            )
        except Exception:  # noqa: BLE001 - bookkeeping never blocks trading
            logger.warning("copy_trades status update failed", exc_info=True)

    async def _mark_copy_filled(self, uid: int, synthetic, result) -> None:
        if self._copy_store is None:
            return
        try:
            entry_price = float(getattr(result, "entry_price", 0.0) or 0.0)
            await self._copy_store.mark_copy_trade_filled(
                int(synthetic.id), uid,
                order_ref=str(getattr(result, "ref", "") or ""),
                size_base=float(getattr(result, "size", 0.0) or 0.0),
                leverage=None,
                entry_price=entry_price if entry_price > 0 else None,
                # the re-anchored stop, not the proposal-time one: the heat cap
                # measures risk as abs(entry - stop) and would otherwise compute
                # it against a level that was never placed. When the venue
                # REJECTED the stop there is no protection on the position, so
                # record no stop rather than book it as bounded risk.
                stop_price=(getattr(synthetic, "stop_loss", None)
                            if getattr(result, "sl_ok", True) else None),
            )
        except Exception:  # noqa: BLE001
            logger.warning("copy_trades fill update failed", exc_info=True)

    async def _copy_eligible(self, uid: int):
        """Prefs for an eligible user, else None."""
        verified = await self._verification_db.get_verified(uid)
        if verified is None or not verified.is_active:
            return None
        conn = await self._venue.get_connection(uid)
        if conn is None or not conn.is_active:
            return None
        prefs = await self._prefs_db.get_or_default(
            uid, default_pct=self._config.default_size_pct,
        )
        return prefs if prefs.ready else None

    def _fmt_copy_preview(
        self, sig, leverage: int, source: str, size_pct: float,
    ) -> str:
        entry = getattr(sig, "entry", None)
        tps = list(getattr(sig, "take_profits", None) or [])
        lines = [
            f"Call detected in {source}:",
            f"{getattr(sig, 'pair', '?')} {getattr(sig, 'side', '?')} {leverage}x"
            + (" (their leverage)" if getattr(sig, "leverage", None) else
               " (default; caller said low lev)"),
            f"Entry: {'market' if entry is None else _fmt_num(entry)}",
        ]
        sl = getattr(sig, "stop_loss", None)
        if sl is not None:
            cond = " (conditional in the call - our stop is a hard price)" if getattr(
                sig, "stop_is_conditional", False) else ""
            lines.append(f"SL {_fmt_num(sl)}{cond}")
        if tps:
            lines.append("TPs: " + " / ".join(_fmt_num(t) for t in tps))
        else:
            lines.append("TPs: none stated - entry + stop only")
        if getattr(sig, "side_inferred", False):
            lines.append("(direction inferred from the stop vs entry)")
        note = getattr(sig, "note", "") or ""
        if note:
            lines.append(note)
        lines.append(
            "\nReply /autotrade copy confirm within 15 min to place it "
            f"at your {size_pct:g}% size."
        )
        return "\n".join(lines)

    async def apply_tps(
        self, uid: int, *, pair: str, take_profits: list[float],
    ) -> None:
        """Lay the signal's TP ladder onto an existing open position (the
        /autotrade tps command). Same eligibility gates as a fire, honors
        dry-run, never raises into the caller."""
        if uid not in self._config.allowlist:
            return
        verified = await self._verification_db.get_verified(uid)
        if verified is None or not verified.is_active:
            return
        conn = await self._venue.get_connection(uid)
        if conn is None or not conn.is_active:
            await self._dm(uid, "Connect first: /autotrade connect")
            return
        prefs = await self._prefs_db.get_or_default(
            uid, default_pct=self._config.default_size_pct,
        )
        if not prefs.ready:
            await self._dm(uid, "Enable autotrade first: /autotrade on")
            return
        if not take_profits:
            await self._dm(uid, "No take-profit prices to place.")
            return

        if self._config.dry_run:
            tps = " / ".join(_fmt_num(t) for t in take_profits)
            await self._dm(
                uid,
                f"[DRY RUN] Would ladder TPs on your open {pair} position "
                f"at {tps}. Nothing placed.",
            )
            return

        try:
            result = await self._venue.place_tp_ladder(
                uid, pair=pair, take_profits=take_profits,
            )
        except VenueError as e:
            await self._dm(uid, f"TP ladder failed on {pair}: {e}")
            return
        except Exception:  # noqa: BLE001
            logger.exception("apply_tps crashed for user=%s", uid)
            await self._dm(uid, f"TP ladder hit an unexpected error on {pair}.")
            return

        direction = "" if result.size <= 0 else f" ({_fmt_num(result.size)} contracts)"
        parts = ", ".join(f"{_fmt_num(sz)}@{_fmt_num(px)}" for px, sz in result.tp_legs)
        status = "" if result.tp_ok else "\nSome legs FAILED - check the exchange."
        await self._dm(
            uid,
            f"TP ladder set on {result.coin}{direction}:\n{parts}{status}\n"
            f"{self._manage_hint()}",
        )

    async def _dm(self, user_id: int, text: str) -> None:
        try:
            await self._send_dm(user_id, text)
        except Exception:  # noqa: BLE001 - a DM failure must not break the flow
            logger.warning("autotrade DM to %s failed", user_id, exc_info=True)

    async def _maybe_fire(
        self, uid: int, signal, signal_id: int, side: str,
    ):
        """Runs the full gate + place path. Returns the VenueResult when an
        order was actually placed, else None (skips, dry-run, failures).
        Callers that don't care (signals, manual fire) ignore the value."""
        # --- eligibility ---
        verified = await self._verification_db.get_verified(uid)
        if verified is None or not verified.is_active:
            return
        conn = await self._venue.get_connection(uid)
        if conn is None or not conn.is_active:
            return
        prefs = await self._prefs_db.get_or_default(
            uid, default_pct=self._config.default_size_pct,
        )
        if not prefs.ready:
            return

        # --- dedupe claim (idempotent per (user, signal)) ---
        if not await self._prefs_db.try_claim_fire(uid, signal_id):
            return

        pair = getattr(signal, "pair", "") or ""
        # The full take-profit ladder in order (tp1, tp2, tp3), skipping any
        # the signal didn't set. The venue scales the size out across these.
        take_profits = [
            float(t)
            for t in (
                getattr(signal, "tp1", None),
                getattr(signal, "tp2", None),
                getattr(signal, "tp3", None),
            )
            if t is not None
        ]
        stop_loss = getattr(signal, "stop_loss", None)
        req_leverage = getattr(signal, "leverage", None) or 0

        # --- sizing: percent of available balance ---
        try:
            balance = await self._venue.get_balance(uid)
        except VenueError as e:
            await self._prefs_db.release_fire(uid, signal_id)
            await self._dm(uid, f"Autotrade skipped {pair}: could not read balance ({e}).")
            return
        # Wallet-copy proposals carry the tracked wallet's equity fraction;
        # it can only shrink the trade, never exceed the user's own pref.
        size_pct = prefs.size_pct
        override = getattr(signal, "size_pct_override", None)
        if override is not None and float(override) > 0:
            size_pct = min(float(override), prefs.size_pct)
        collateral = balance * (size_pct / 100.0)
        if self._max_collateral_usdc and self._max_collateral_usdc > 0:
            collateral = min(collateral, self._max_collateral_usdc)
        collateral = min(collateral, balance)
        if collateral < self._config.min_collateral_usdc:
            await self._prefs_db.release_fire(uid, signal_id)
            await self._dm(
                uid,
                f"Autotrade skipped {pair}: balance too low "
                f"(${balance:,.2f} available, need ~${self._config.min_collateral_usdc:g}).",
            )
            return

        try:
            plan = await self._venue.plan(
                pair=pair,
                direction=side,
                collateral_usdc=collateral,
                requested_leverage=int(req_leverage),
                max_leverage=self._config.max_leverage,
            )
        except VenueError as e:
            await self._prefs_db.release_fire(uid, signal_id)
            await self._dm(uid, f"Autotrade skipped {pair}: {e}.")
            return
        if plan is None:
            await self._prefs_db.release_fire(uid, signal_id)
            await self._dm(
                uid,
                f"Autotrade skipped {pair}: not listed on {self._venue.name}, "
                f"or the size falls below the venue minimum.",
            )
            return

        # --- copy proposals: re-anchor the protective levels to OUR entry ---
        # The wallet watcher derives the stop and the TP ladder from the LEADER's
        # Hyperliquid entry price, but we fill at market, on a different venue,
        # up to 15 minutes later. Passing those levels straight through puts the
        # stop a fixed distance from a price we never paid. A move in our FAVOUR
        # is the dangerous one and nothing caught it: the deviation gate only
        # cancels adverse moves, so a favourable drift silently compressed the
        # stop below the intended ATR multiple, and past 1x atr_mult it landed on
        # the wrong side of the fill, where Blofin either triggers it immediately
        # or rejects it and leaves the position with no stop at all.
        # plan.price is the price the venue just sized against and reports the
        # fill at, so re-deriving from it keeps the risk at exactly
        # atr_mult x ATR from where we actually get in.
        risk_per_unit = getattr(signal, "copy_risk_per_unit", None)
        if risk_per_unit and float(risk_per_unit) > 0 and plan.price > 0:
            risk = float(risk_per_unit)
            if plan.is_long:
                stop_loss = plan.price - risk
                take_profits = [plan.price + k * risk for k in (1, 2, 3)]
            else:
                stop_loss = plan.price + risk
                take_profits = [plan.price - k * risk for k in (1, 2, 3)]
            wanted_legs = 3
            take_profits = [t for t in take_profits if t > 0]
            if stop_loss <= 0 or risk >= plan.price:
                # ATR at or wider than the price itself. On a long that gives a
                # non-positive stop; on a SHORT the stop stays positive but sits
                # above 2x the price, which is unreachable, and every TP prices
                # below zero and is filtered away, leaving a live position with
                # an unreachable stop and no ladder. Both are the same defect,
                # so refuse on the risk distance rather than on the sign.
                await self._prefs_db.release_fire(uid, signal_id)
                await self._dm(
                    uid,
                    f"Autotrade skipped {plan.coin}: recent volatility "
                    f"({_fmt_num(risk)}) is as wide as the price "
                    f"({_fmt_num(plan.price)}), so no reachable stop or take-profit "
                    f"ladder can be placed.",
                )
                return
            if len(take_profits) < wanted_legs:
                # the venue re-normalises the scale-out weights over whatever legs
                # survive, so a silently dropped leg puts an outsized share of the
                # position on TP1 instead of laddering out of it.
                logger.info(
                    "re-anchor dropped %d TP leg(s) for %s (price=%s risk=%s)",
                    wanted_legs - len(take_profits), plan.coin, plan.price, risk,
                )
            # keep the proposal object honest: confirm_copy reads stop_loss back
            # off it to record what was actually placed.
            signal.stop_loss = stop_loss
            padded = (take_profits + [None, None, None])[:3]
            signal.tp1, signal.tp2, signal.tp3 = padded

        # --- a stop on the wrong side of the entry is never placeable ---
        # cabal_parser validates stop side at parse time, but price moves between
        # the call and the fill, and the wallet path had no equivalent check at
        # all. Refusing costs one missed trade; placing costs an instant stop-out
        # (two taker fees) or an unprotected position.
        if stop_loss is not None and plan.price > 0:
            wrong_side = (
                stop_loss >= plan.price if plan.is_long
                else stop_loss <= plan.price
            )
            if wrong_side:
                await self._prefs_db.release_fire(uid, signal_id)
                await self._dm(
                    uid,
                    f"Autotrade skipped {plan.coin}: the stop "
                    f"({_fmt_num(stop_loss)}) is on the wrong side of the "
                    f"current price ({_fmt_num(plan.price)}). Placing this "
                    f"would stop out instantly or leave the position with no "
                    f"stop.",
                )
                logger.info(
                    "wrong-side stop blocked %s for user=%s (stop=%s price=%s)",
                    plan.coin, uid, stop_loss, plan.price,
                )
                return

        # --- account-level risk guard (only when enabled AND venue-supported) ---
        # Fail-closed: an unreadable account state blocks the trade. Runs before
        # both branches so a dry-run soak shows exactly what it would block.
        verdict = None
        if self._guard_on:
            verdict = await self._risk.check(uid, plan)

        # --- dry-run: preview only, no order, no daily-slot spend ---
        if self._config.dry_run:
            preview = self._fmt_preview(plan, take_profits, stop_loss)
            if verdict is not None and not verdict.allowed:
                preview += f"\nRISK GUARD would block this: {verdict.reason}"
            await self._dm(uid, preview)
            return

        if verdict is not None and not verdict.allowed:
            await self._prefs_db.release_fire(uid, signal_id)
            await self._dm(uid, f"Autotrade blocked {plan.coin}: {verdict.reason}")
            logger.info(
                "risk guard blocked user=%s coin=%s: %s", uid, plan.coin, verdict.reason,
            )
            return

        # --- live: daily cap, then place ---
        if not await self._prefs_db.try_consume_daily_slot(uid, self._config.max_per_day):
            await self._prefs_db.release_fire(uid, signal_id)
            await self._dm(
                uid,
                f"Autotrade skipped {plan.coin}: daily cap "
                f"({self._config.max_per_day} trades) reached.",
            )
            return

        try:
            result = await self._venue.place(
                uid, plan, take_profits=take_profits, stop_loss=stop_loss,
                slippage_bps=self._config.slippage_bps,
            )
        except VenueError as e:
            await self._prefs_db.release_fire(uid, signal_id)
            await self._venue.mark_failure(uid, str(e))
            await self._dm(uid, f"Autotrade failed on {plan.coin}: {e}")
            return
        except Exception as e:  # noqa: BLE001
            await self._prefs_db.release_fire(uid, signal_id)
            await self._venue.mark_failure(uid, repr(e))
            await self._dm(uid, f"Autotrade failed on {plan.coin}: unexpected error.")
            logger.exception("place crashed for user=%s", uid)
            return

        await self._venue.mark_success(uid)
        await self._dm(uid, self._fmt_success(plan, result, take_profits, stop_loss))
        return result

    # ---- DM formatting -----------------------------------------------------

    def _manage_hint(self) -> str:
        url = _MANAGE_URL.get(getattr(self._venue, "name", ""), "your exchange")
        return f"Manage the position on {url}."

    def _fmt_preview(
        self, plan: VenuePlan, take_profits: list[float], stop_loss: float | None,
    ) -> str:
        direction = "LONG" if plan.is_long else "SHORT"
        lines = [
            "[DRY RUN] Perp Bot autotrade",
            f"Would open {plan.coin} {direction} {plan.leverage}x",
            f"Size {_fmt_num(plan.size)} {plan.coin} (~${plan.notional_usd:,.0f}) "
            f"at ~${_fmt_num(plan.price)}",
        ]
        if take_profits:
            tps = " / ".join(_fmt_num(t) for t in take_profits)
            label = "TP ladder (scaled out)" if len(take_profits) > 1 else "TP"
            lines.append(f"{label}: {tps}")
        if stop_loss is not None:
            lines.append(f"SL {_fmt_num(stop_loss)}")
        lines.append("No order placed (dry-run).")
        return "\n".join(lines)

    def _fmt_success(
        self, plan: VenuePlan, result, take_profits: list[float],
        stop_loss: float | None,
    ) -> str:
        direction = "LONG" if plan.is_long else "SHORT"
        lines = [
            "Autotrade filled",
            f"{plan.coin} {direction} {plan.leverage}x",
            f"Size {_fmt_num(plan.size)} (~${plan.notional_usd:,.0f})",
        ]
        sl_ok = getattr(result, "sl_ok", True)
        tp_ok = getattr(result, "tp_ok", True)
        legs = getattr(result, "tp_legs", None) or []
        if stop_loss is not None:
            lines.append(f"SL {_fmt_num(stop_loss)}: {'set' if sl_ok else 'FAILED - set it manually'}")
        if legs:
            parts = ", ".join(f"{_fmt_num(sz)}@{_fmt_num(px)}" for px, sz in legs)
            status = "set" if tp_ok else "PARTIAL - check the exchange"
            label = "TP ladder" if len(legs) > 1 else "TP"
            lines.append(f"{label} ({parts}): {status}")
        elif take_profits:
            lines.append("TP: none placed - set them manually")
        lines.append(self._manage_hint())
        return "\n".join(lines)
