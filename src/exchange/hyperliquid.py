"""Hyperliquid API wrapper — connection, auth, order placement."""

import functools
import logging
import random
import time
from typing import Any

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.constants import TESTNET_API_URL, MAINNET_API_URL

logger = logging.getLogger(__name__)

TRANSIENT_EXCEPTIONS = (ConnectionError, TimeoutError, OSError)
RATE_LIMIT_STRINGS = ("rate limit", "too many requests", "429")


def retry_on_transient(max_retries: int = 3, base_delay: float = 1.0):
    """Decorator that retries on transient network errors with exponential backoff + jitter."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except TRANSIENT_EXCEPTIONS as e:
                    last_exc = e
                    if attempt == max_retries:
                        raise
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                    logger.warning(
                        "Transient error in %s (attempt %d/%d), retrying in %.1fs: %s",
                        func.__name__, attempt + 1, max_retries, delay, e,
                    )
                    time.sleep(delay)
                except Exception as e:
                    # Check for rate-limit messages in string representation
                    err_str = str(e).lower()
                    if any(s in err_str for s in RATE_LIMIT_STRINGS):
                        last_exc = e
                        if attempt == max_retries:
                            raise
                        delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                        logger.warning(
                            "Rate limited in %s (attempt %d/%d), retrying in %.1fs: %s",
                            func.__name__, attempt + 1, max_retries, delay, e,
                        )
                        time.sleep(delay)
                    else:
                        raise
            raise last_exc  # pragma: no cover
        return wrapper
    return decorator

NETWORK_URLS = {
    "testnet": TESTNET_API_URL,
    "mainnet": MAINNET_API_URL,
}


class HyperliquidClient:
    """Unified wrapper around Hyperliquid Info + Exchange clients.

    Provides account info, balance checks, and order management.
    Defaults to testnet — mainnet requires explicit opt-in.
    """

    def __init__(
        self,
        account_address: str,
        private_key: str,
        network: str = "testnet",
    ):
        """
        Args:
            account_address: 0x-prefixed master account address (used for queries).
            private_key: 0x-prefixed API wallet private key (used for signing).
            network: 'testnet' or 'mainnet'.

        Note:
            Hyperliquid separates signing (API wallet) from account ownership
            (master address). Queries must use the master account address.
            The API wallet only signs transactions on its behalf.
        """
        if network not in NETWORK_URLS:
            raise ValueError(f"Unknown network '{network}'. Use 'testnet' or 'mainnet'.")

        self._account_address = account_address
        self._network = network
        self._base_url = NETWORK_URLS[network]

        self._wallet = eth_account.Account.from_key(private_key)
        self._info = Info(base_url=self._base_url, skip_ws=True)
        self._exchange = Exchange(
            wallet=self._wallet,
            base_url=self._base_url,
            account_address=account_address,
        )

        logger.info(
            "HyperliquidClient initialized: network=%s account=%s api_wallet=%s",
            network,
            account_address,
            self._wallet.address,
        )

    @property
    def network(self) -> str:
        return self._network

    @property
    def account_address(self) -> str:
        """Master account address (used for queries)."""
        return self._account_address

    @property
    def api_wallet_address(self) -> str:
        """API wallet address (used for signing)."""
        return self._wallet.address

    @property
    def info(self) -> Info:
        """Raw Info client for advanced queries."""
        return self._info

    @property
    def exchange(self) -> Exchange:
        """Raw Exchange client for advanced operations."""
        return self._exchange

    # ------------------------------------------------------------------
    # Account queries
    # ------------------------------------------------------------------

    @retry_on_transient()
    def get_account_state(self) -> dict[str, Any]:
        """Return full account state (positions, margin summary, withdrawable)."""
        return self._info.user_state(self._account_address)

    @retry_on_transient()
    def get_spot_balances(self) -> list[dict[str, str]]:
        """Return non-zero spot token balances (includes USDC under portfolio margin)."""
        state = self._info.spot_user_state(self._account_address)
        balances = []
        for b in state.get("balances", []):
            total = float(b.get("total", 0))
            if total != 0:
                balances.append({
                    "coin": b.get("coin"),
                    "total": b.get("total"),
                    "hold": b.get("hold"),
                })
        return balances

    @retry_on_transient()
    def get_balance(self) -> dict[str, str]:
        """Return unified account summary.

        Under portfolio margin, USDC lives in the spot clearinghouse
        but is available for perps trading. This method returns both the
        perps margin summary and the spot USDC balance for a complete picture.
        """
        perp_state = self.get_account_state()
        summary = perp_state.get("marginSummary", {})

        # Spot USDC balance (where funds actually sit under portfolio margin)
        spot_balances = self.get_spot_balances()
        usdc_balance = "0"
        for b in spot_balances:
            if b["coin"] == "USDC":
                usdc_balance = b["total"]
                break

        return {
            "usdc_balance": usdc_balance,
            "account_value": summary.get("accountValue", "0"),
            "total_margin_used": summary.get("totalMarginUsed", "0"),
            "total_position_value": summary.get("totalNtlPos", "0"),
            "withdrawable": perp_state.get("withdrawable", "0"),
        }

    @retry_on_transient()
    def get_open_positions(self) -> list[dict[str, Any]]:
        """Return list of non-zero positions."""
        state = self.get_account_state()
        positions = []
        for p in state.get("assetPositions", []):
            pos = p.get("position", {})
            size = float(pos.get("szi", 0))
            if size != 0:
                positions.append({
                    "coin": pos.get("coin"),
                    "size": size,
                    "entry_price": pos.get("entryPx"),
                    "unrealized_pnl": pos.get("unrealizedPnl"),
                    "leverage": pos.get("leverage"),
                    "liquidation_price": pos.get("liquidationPx"),
                })
        return positions

    @retry_on_transient()
    def get_open_orders(self) -> list[dict[str, Any]]:
        """Return all open orders."""
        return self._info.open_orders(self._account_address)

    @retry_on_transient()
    def get_all_mids(self) -> dict[str, str]:
        """Return current mid prices for all traded assets."""
        return self._info.all_mids()

    @retry_on_transient()
    def get_asset_meta(self) -> dict[str, dict]:
        """Return per-coin metadata from the exchange, cached for the session.

        Returns:
            Dict keyed by coin name, e.g.:
            {"ETH": {"szDecimals": 4, "maxLeverage": 25}, ...}
        """
        if not hasattr(self, "_asset_meta_cache"):
            raw = self._info.meta()
            self._asset_meta_cache = {
                asset["name"]: asset for asset in raw["universe"]
            }
            logger.info("Cached metadata for %d assets", len(self._asset_meta_cache))
        return self._asset_meta_cache
