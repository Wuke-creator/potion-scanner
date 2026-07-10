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
    ):
        self._config = config
        self._max_collateral_usdc = max_collateral_usdc
        self._venue = venue
        self._prefs_db = prefs_db
        self._verification_db = verification_db
        self._send_dm = send_dm
        # The risk guard only runs when explicitly enabled AND the venue can
        # supply an account snapshot. Blofin can't yet, so it's off there.
        self._guard_on = bool(
            getattr(config, "risk_enabled", True)
            and getattr(venue, "supports_risk_guard", False)
        )
        self._risk = AutotradeRiskGuard(config=config, venue=venue, prefs_db=prefs_db)

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

    async def _maybe_fire(self, uid: int, signal, signal_id: int, side: str) -> None:
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
        collateral = balance * (prefs.size_pct / 100.0)
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
