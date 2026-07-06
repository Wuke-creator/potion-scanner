"""Signal-driven autotrade engine (Hyperliquid).

When the router records a new "Perp Bot Calls" signal it calls
``on_new_signal``. For each allowlisted, connected, opted-in user the engine:

  1. checks eligibility (allowlist AND Elite-active AND agent connected
     AND /autotrade on + disclosure accepted),
  2. atomically claims (user, signal) so a re-broadcast can't double-fire,
  3. sizes collateral as ``withdrawable_usdc * size_pct`` (clamped),
  4. builds a Hyperliquid plan (coin resolution, price, szDecimals-floored
     size, leverage capped by the signal, the user max, and the asset max),
  5. in dry-run: DMs the intended trade and places nothing,
     live: consumes a daily-cap slot, places the trade, DMs the result.

Every user is isolated: one user's failure never blocks another. Nothing
fires unless ``config.enabled`` and the allowlist is non-empty; the router
only calls this for the configured source channel.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from src.config.settings import AutotradeConfig
from src.trading.autotrade_risk import AutotradeRiskGuard
from src.trading.hyperliquid_client import HyperliquidClient, HyperliquidError, TradePlan

logger = logging.getLogger(__name__)

SendDM = Callable[[int, str], Awaitable[None]]


def _fmt_num(value: float) -> str:
    return f"{value:g}"


class AutotradeEngine:
    def __init__(
        self,
        *,
        config: AutotradeConfig,
        max_collateral_usdc: float,
        client: HyperliquidClient,
        delegates_db,
        prefs_db,
        verification_db,
        send_dm: SendDM,
    ):
        self._config = config
        self._max_collateral_usdc = max_collateral_usdc
        self._client = client
        self._delegates_db = delegates_db
        self._prefs_db = prefs_db
        self._verification_db = verification_db
        self._send_dm = send_dm
        self._risk = AutotradeRiskGuard(config=config, client=client, prefs_db=prefs_db)

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
        delegate = await self._delegates_db.get(uid)
        if delegate is None or not delegate.is_active:
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
        tp1 = getattr(signal, "tp1", None)
        stop_loss = getattr(signal, "stop_loss", None)
        req_leverage = getattr(signal, "leverage", None) or 0

        # --- sizing: percent of withdrawable USDC ---
        balance = await self._client.get_available_usdc(delegate.trader_address)
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

        plan = await self._client.plan_trade(
            pair=pair,
            direction=side,
            collateral_usdc=collateral,
            requested_leverage=int(req_leverage),
            max_leverage=self._config.max_leverage,
        )
        if plan is None:
            await self._prefs_db.release_fire(uid, signal_id)
            await self._dm(
                uid,
                f"Autotrade skipped {pair}: not listed on Hyperliquid, "
                f"or the size falls below the $10 minimum.",
            )
            return

        # --- account-level risk guard (passivbot-derived; see autotrade_risk) ---
        # Runs before BOTH branches so the dry-run soak shows exactly what the
        # guard would have blocked live. Fail-closed: an unreadable account
        # state blocks the trade rather than waving it through.
        verdict = await self._risk.check(uid, delegate.trader_address, plan)

        # --- dry-run: preview only, no order, no daily-slot spend ---
        if self._config.dry_run:
            preview = self._fmt_preview(plan, tp1, stop_loss)
            if not verdict.allowed:
                preview += f"\nRISK GUARD would block this: {verdict.reason}"
            await self._dm(uid, preview)
            return

        if not verdict.allowed:
            await self._prefs_db.release_fire(uid, signal_id)
            await self._dm(
                uid,
                f"Autotrade blocked {plan.coin}: {verdict.reason}",
            )
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

        agent_key = await self._delegates_db.get_plaintext_key(uid)
        if not agent_key:
            await self._prefs_db.release_fire(uid, signal_id)
            return

        try:
            result = await self._client.place_trade(
                agent_private_key=agent_key,
                master_address=delegate.trader_address,
                plan=plan,
                take_profit=tp1,
                stop_loss=stop_loss,
                slippage_bps=self._config.slippage_bps,
            )
        except HyperliquidError as e:
            await self._prefs_db.release_fire(uid, signal_id)
            await self._delegates_db.mark_trade_failure(uid, str(e))
            await self._dm(uid, f"Autotrade failed on {plan.coin}: {e}")
            return
        except Exception as e:  # noqa: BLE001
            await self._prefs_db.release_fire(uid, signal_id)
            await self._delegates_db.mark_trade_failure(uid, repr(e))
            await self._dm(uid, f"Autotrade failed on {plan.coin}: unexpected error.")
            logger.exception("place_trade crashed for user=%s", uid)
            return

        await self._delegates_db.mark_trade_success(uid)
        await self._dm(uid, self._fmt_success(plan, result, tp1, stop_loss))

    # ---- DM formatting -----------------------------------------------------

    def _fmt_preview(
        self, plan: TradePlan, tp1: float | None, stop_loss: float | None,
    ) -> str:
        direction = "LONG" if plan.is_long else "SHORT"
        lines = [
            "[DRY RUN] Perp Bot autotrade",
            f"Would open {plan.coin} {direction} {plan.leverage}x",
            f"Size {_fmt_num(plan.size)} {plan.coin} (~${plan.notional_usd:,.0f}) "
            f"at ~${_fmt_num(plan.price)}",
        ]
        if tp1 is not None:
            lines.append(f"TP {_fmt_num(tp1)}")
        if stop_loss is not None:
            lines.append(f"SL {_fmt_num(stop_loss)}")
        lines.append("No order placed (dry-run).")
        return "\n".join(lines)

    def _fmt_success(
        self, plan: TradePlan, result, tp1: float | None, stop_loss: float | None,
    ) -> str:
        direction = "LONG" if plan.is_long else "SHORT"
        lines = [
            "Autotrade filled",
            f"{plan.coin} {direction} {plan.leverage}x",
            f"Size {_fmt_num(plan.size)} (~${plan.notional_usd:,.0f})",
        ]
        sl_ok = getattr(result, "sl_ok", True)
        tp_ok = getattr(result, "tp_ok", True)
        if stop_loss is not None:
            lines.append(f"SL {_fmt_num(stop_loss)}: {'set' if sl_ok else 'FAILED - set it manually'}")
        if tp1 is not None:
            lines.append(f"TP {_fmt_num(tp1)}: {'set' if tp_ok else 'FAILED - set it manually'}")
        lines.append("Manage the position on app.hyperliquid.xyz.")
        return "\n".join(lines)
