"""Pre-trade risk guard for the autotrade engine (passivbot-derived).

The engine's existing controls (size percent, collateral caps, leverage caps,
daily trade cap) bound each INDIVIDUAL trade. What they do not bound is the
account: nothing stops exposure from stacking trade after trade until the
wallet is one adverse move from liquidation, and nothing slows the engine
down while an account is bleeding. Those are the two failure modes passivbot's
risk layer exists for, and this module ports the applicable parts
(github.com/enarjord/passivbot, Unlicense; docs/risk_management.md):

  1. Wallet exposure, per symbol and total. passivbot's core metric:
       wallet_exposure = position_notional / account_value
     New entries are blocked when they would push one coin past
     ``risk_symbol_we_limit`` or the whole account past ``risk_total_we_limit``.
     At WE 2.0 a ~50 percent adverse move is account-ending, which is why the
     defaults here are 1.0 per symbol and 2.0 total.

  2. Balance-peak drawdown brake (passivbot's max_realized_loss_pct gate,
     repurposed for a signal follower): the guard tracks each account's peak
     value and blocks NEW entries while the account sits more than
     ``risk_max_drawdown_pct`` below that peak. Open positions and their
     SL/TP orders are untouched; this only stops digging.

  3. No stacking: one open position per coin. A second signal on a coin we
     already hold is blocked instead of averaging in (passivbot manages
     grid re-entries deliberately; a fire-and-forget signal follower that
     stacks entries is just concentrating risk).

Fail-closed: if the account snapshot cannot be fetched, live trades are
blocked (reason says so). A guard that fails open is not a guard.

Peak values live in the prefs DB (autotrade_equity_peaks). Deposits raise
account value and therefore the peak; withdrawals do NOT lower the peak, so
after a large withdrawal the drawdown brake can engage until the peak is
reset (documented operator action: delete the row).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskVerdict:
    allowed: bool
    reason: str = ""

    @staticmethod
    def ok() -> "RiskVerdict":
        return RiskVerdict(allowed=True)

    @staticmethod
    def blocked(reason: str) -> "RiskVerdict":
        return RiskVerdict(allowed=False, reason=reason)


class AutotradeRiskGuard:
    """Sequential pre-trade checks; the first failure wins."""

    def __init__(self, *, config, client, prefs_db):
        self._config = config
        self._client = client
        self._prefs_db = prefs_db

    async def check(self, uid: int, master_address: str, plan) -> RiskVerdict:
        snapshot = await self._client.get_account_snapshot(master_address)
        if snapshot is None or snapshot.account_value <= 0:
            return RiskVerdict.blocked(
                "risk data unavailable (could not read account state); "
                "trade blocked, not skipped silently"
            )

        acct = snapshot.account_value
        positions = snapshot.positions  # {coin: abs notional usd}
        coin_notional = positions.get(plan.coin, 0.0)

        # --- no stacking: one position per coin ---
        if self._config.risk_no_stacking and coin_notional > 0:
            return RiskVerdict.blocked(
                f"{plan.coin} position already open (~${coin_notional:,.0f}); "
                f"stacking entries is disabled"
            )

        # --- per-symbol wallet exposure ---
        symbol_limit = float(self._config.risk_symbol_we_limit or 0)
        if symbol_limit > 0:
            symbol_we = (coin_notional + plan.notional_usd) / acct
            if symbol_we > symbol_limit:
                return RiskVerdict.blocked(
                    f"{plan.coin} wallet exposure {symbol_we:.2f}x would exceed "
                    f"the {symbol_limit:g}x per-symbol limit "
                    f"(account ${acct:,.0f}, trade ~${plan.notional_usd:,.0f})"
                )

        # --- total wallet exposure ---
        total_limit = float(self._config.risk_total_we_limit or 0)
        if total_limit > 0:
            total_notional = sum(positions.values())
            total_we = (total_notional + plan.notional_usd) / acct
            if total_we > total_limit:
                return RiskVerdict.blocked(
                    f"total wallet exposure {total_we:.2f}x would exceed the "
                    f"{total_limit:g}x limit "
                    f"(open ~${total_notional:,.0f} + trade ~${plan.notional_usd:,.0f} "
                    f"on account ${acct:,.0f})"
                )

        # --- balance-peak drawdown brake ---
        dd_limit = float(self._config.risk_max_drawdown_pct or 0)
        if dd_limit > 0:
            peak = await self._prefs_db.bump_equity_peak(uid, acct)
            if peak > 0:
                drawdown_pct = (peak - acct) / peak * 100.0
                if drawdown_pct > dd_limit:
                    return RiskVerdict.blocked(
                        f"account is {drawdown_pct:.1f}% below its ${peak:,.0f} peak "
                        f"(brake at {dd_limit:g}%); no new entries until it recovers "
                        f"or the peak is reset"
                    )

        return RiskVerdict.ok()
