"""Position size calculator.

Determines how much USD to allocate per trade based on:
  1. Account balance
  2. Strategy preset (size_pct)
  3. Risk level override (size_by_risk)
  4. Risk limits (max_position_size_usd, min_order_usd)
"""

import logging

from src.config.settings import RiskConfig, StrategyConfig, StrategyPreset

logger = logging.getLogger(__name__)


class PositionSizeError(Exception):
    """Raised when a valid position size cannot be calculated."""


def calculate_position_size(
    balance_usd: float,
    risk_level: str,
    preset: StrategyPreset,
    strategy_config: StrategyConfig,
    risk_config: RiskConfig,
) -> float:
    """Calculate the USD position size for a trade.

    Resolution order for size_pct:
      1. size_by_risk[risk_level] if the risk level exists in the map
      2. preset.size_pct (the preset's default)

    The result is then clamped to risk limits.

    Args:
        balance_usd: Current account balance in USD.
        risk_level: Signal risk level ("LOW", "MEDIUM", "HIGH").
        preset: The active strategy preset.
        strategy_config: Strategy config (contains size_by_risk overrides).
        risk_config: Risk limits (max/min position sizes).

    Returns:
        Position size in USD, ready to pass to build_orders().

    Raises:
        PositionSizeError: If the calculated size is below the exchange minimum.
    """
    # Resolve size percentage
    size_pct = strategy_config.size_by_risk.get(risk_level, preset.size_pct)

    # Calculate raw USD size
    raw_size = balance_usd * (size_pct / 100.0)

    # Clamp to max position size
    clamped_size = min(raw_size, risk_config.max_position_size_usd)

    # Check minimum
    if clamped_size < risk_config.min_order_usd:
        raise PositionSizeError(
            f"Position size ${clamped_size:.2f} ({size_pct}% of ${balance_usd:.2f}) "
            f"is below minimum ${risk_config.min_order_usd:.2f}"
        )

    logger.info(
        "Position size: $%.2f (%.1f%% of $%.2f, risk=%s, clamped to max=$%.2f)",
        clamped_size,
        size_pct,
        balance_usd,
        risk_level,
        risk_config.max_position_size_usd,
    )

    return clamped_size
