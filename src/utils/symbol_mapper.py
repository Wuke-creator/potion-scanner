"""Pair name to Hyperliquid symbol format mapping.

Potion Perps signals use pairs like 'ZK/USDT' or '1000BONK/USDT'.
Hyperliquid uses coin names like 'ZK' or 'kBONK'.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Potion Perps "1000X" prefix → Hyperliquid "kX" prefix
_KILO_PREFIX = "1000"


def potion_to_hyperliquid(pair: str, available_coins: dict[str, Any] | None = None) -> str:
    """Convert a Potion Perps pair to a Hyperliquid coin name.

    Args:
        pair: Pair string from signal, e.g. 'ZK/USDT', '1000BONK/USDT'.
        available_coins: Optional dict of available coins (from get_all_mids())
            used to validate the result. If None, no validation is performed.

    Returns:
        Hyperliquid coin name, e.g. 'ZK', 'kBONK'.

    Raises:
        ValueError: If the coin is not available on Hyperliquid (when validated).
    """
    # Strip quote currency (always /USDT for perps)
    base = pair.split("/")[0].strip().upper()

    # Handle 1000X → kX mapping (e.g. 1000BONK → kBONK, 1000PEPE → kPEPE)
    if base.startswith(_KILO_PREFIX):
        coin = "k" + base[len(_KILO_PREFIX):]
    else:
        coin = base

    if available_coins is not None and coin not in available_coins:
        raise ValueError(
            f"Coin '{coin}' (from pair '{pair}') not found on Hyperliquid"
        )

    return coin
