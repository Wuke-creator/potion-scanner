"""Config loader and validation.

Loads settings from two sources:
  1. .env file — secrets (Hyperliquid credentials, Discord token)
  2. config.yaml — all non-secret settings (strategy presets, risk, logging, etc.)

Returns a typed Config dataclass with validated fields.

Multi-user: load_config() accepts a config_dir and env_file per user.
For now, defaults to project-root config/ and .env.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when config is missing or invalid."""


# ------------------------------------------------------------------
# Config dataclasses
# ------------------------------------------------------------------

@dataclass
class ExchangeConfig:
    """Hyperliquid connection settings. Credentials come from .env."""

    network: str = "testnet"
    account_address: str = ""
    api_wallet: str = ""
    api_secret: str = ""


@dataclass
class InputConfig:
    """Signal input adapter settings."""

    adapter: str = "simulation"
    signals_dir: str = "signals/incoming/"
    simulation_dir: str = "signals/samples/"
    simulation_delay_sec: float = 5.0


@dataclass
class StrategyPreset:
    """A named bundle of trade management parameters.

    This is the core abstraction: a "strategy" is just a combination of
    TP management, SL management, and position sizing. Users can define
    their own presets or use built-in ones.
    """

    tp_split: list[float] = field(default_factory=lambda: [0.33, 0.33, 0.34])
    move_sl_to_breakeven_after: str = "tp1"  # "tp1" | "tp2" | "never"
    size_pct: float = 2.0                     # % of account balance per trade


# Built-in presets matching the 7 backtested approaches
BUILTIN_PRESETS: dict[str, StrategyPreset] = {
    "runner": StrategyPreset(
        tp_split=[0.33, 0.33, 0.34],
        move_sl_to_breakeven_after="tp1",
        size_pct=2.0,
    ),
    "conservative": StrategyPreset(
        tp_split=[1.0, 0.0, 0.0],
        move_sl_to_breakeven_after="never",
        size_pct=2.0,
    ),
    "tp2_exit": StrategyPreset(
        tp_split=[0.5, 0.5, 0.0],
        move_sl_to_breakeven_after="tp1",
        size_pct=2.0,
    ),
    "tp3_hold": StrategyPreset(
        tp_split=[0.0, 0.0, 1.0],
        move_sl_to_breakeven_after="tp1",
        size_pct=2.0,
    ),
    "breakeven_filter": StrategyPreset(
        tp_split=[0.33, 0.33, 0.34],
        move_sl_to_breakeven_after="tp1",
        size_pct=1.5,
    ),
    "small_runner": StrategyPreset(
        tp_split=[0.33, 0.33, 0.34],
        move_sl_to_breakeven_after="tp1",
        size_pct=0.5,
    ),
}


@dataclass
class StrategyConfig:
    """Strategy selection and overrides."""

    active_preset: str = "runner"
    auto_execute: bool = False
    max_leverage: int = 20
    size_by_risk: dict[str, float] = field(
        default_factory=lambda: {"LOW": 4.0, "MEDIUM": 2.0, "HIGH": 1.0}
    )
    presets: dict[str, StrategyPreset] = field(default_factory=dict)


@dataclass
class RiskConfig:
    """Risk management limits."""

    max_open_positions: int = 10
    max_daily_loss_pct: float = 10.0
    max_position_size_usd: float = 500.0
    min_order_usd: float = 10.0


@dataclass
class DatabaseConfig:
    """State persistence settings."""

    path: str = "data/trades.db"


@dataclass
class LoggingConfig:
    """Logging settings."""

    level: str = "INFO"
    file: str = "logs/bot.log"


@dataclass
class DiscordConfig:
    """Discord integration settings (Phase 4)."""

    bot_token: str = ""
    channel_id: str = ""
    source_bot_name: str = "Potion Perps"


@dataclass
class Config:
    """Top-level configuration — single source of truth for all bot settings."""

    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    input: InputConfig = field(default_factory=InputConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)

    def get_active_preset(self) -> StrategyPreset:
        """Resolve the currently active strategy preset.

        Looks up in user-defined presets first, then falls back to built-ins.

        Raises:
            ConfigError: If the active preset name is not found.
        """
        name = self.strategy.active_preset
        if name in self.strategy.presets:
            return self.strategy.presets[name]
        if name in BUILTIN_PRESETS:
            return BUILTIN_PRESETS[name]
        raise ConfigError(
            f"Strategy preset '{name}' not found. "
            f"Available: {sorted(set(BUILTIN_PRESETS) | set(self.strategy.presets))}"
        )


# ------------------------------------------------------------------
# Loader
# ------------------------------------------------------------------

def _deep_update(base: dict, overrides: dict) -> dict:
    """Recursively merge overrides into base dict."""
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def _build_presets(raw: dict) -> dict[str, StrategyPreset]:
    """Build StrategyPreset instances from the YAML strategy_presets section."""
    presets = {}
    for name, params in raw.items():
        if not isinstance(params, dict):
            continue
        presets[name] = StrategyPreset(
            tp_split=params.get("tp_split", [0.33, 0.33, 0.34]),
            move_sl_to_breakeven_after=params.get("move_sl_to_breakeven_after", "tp1"),
            size_pct=params.get("size_pct", 2.0),
        )
    return presets


def load_config(
    config_dir: str | Path = "config",
    env_file: str | Path = ".env",
    config_filename: str = "config.yaml",
) -> Config:
    """Load and validate bot configuration.

    Args:
        config_dir: Directory containing config.yaml.
        env_file: Path to .env file with secrets.
        config_filename: Name of the YAML config file.

    Returns:
        Fully populated Config dataclass.

    Raises:
        ConfigError: If required credentials are missing or values are invalid.
    """
    # --- Load .env secrets ---
    env_path = Path(env_file)
    if env_path.exists():
        load_dotenv(env_path, override=True)
        logger.info("Loaded env from %s", env_path)
    else:
        logger.warning("No .env file found at %s — relying on environment", env_path)

    # --- Load YAML config ---
    yaml_path = Path(config_dir) / config_filename
    yaml_data: dict = {}
    if yaml_path.exists():
        with open(yaml_path) as f:
            yaml_data = yaml.safe_load(f) or {}
        logger.info("Loaded config from %s", yaml_path)
    else:
        logger.warning("No config file at %s — using defaults", yaml_path)

    # --- Build presets (user-defined from YAML + built-ins) ---
    user_presets = _build_presets(yaml_data.get("strategy_presets", {}))

    # --- Build strategy config ---
    strategy_yaml = yaml_data.get("strategy", {})
    strategy_config = StrategyConfig(
        active_preset=strategy_yaml.get("active_preset", "runner"),
        auto_execute=strategy_yaml.get("auto_execute", False),
        max_leverage=strategy_yaml.get("max_leverage", 20),
        size_by_risk=strategy_yaml.get("size_by_risk", {"LOW": 4.0, "MEDIUM": 2.0, "HIGH": 1.0}),
        presets=user_presets,
    )

    # --- Build Config from YAML + env ---
    exchange_yaml = yaml_data.get("exchange", {})
    config = Config(
        exchange=ExchangeConfig(
            network=exchange_yaml.get("network", "testnet"),
            account_address=os.getenv("HL_ACCOUNT_ADDRESS", ""),
            api_wallet=os.getenv("HL_API_WALLET", ""),
            api_secret=os.getenv("HL_API_SECRET", ""),
        ),
        input=_build_dataclass(InputConfig, yaml_data.get("input", {})),
        strategy=strategy_config,
        risk=_build_dataclass(RiskConfig, yaml_data.get("risk", {})),
        database=_build_dataclass(DatabaseConfig, yaml_data.get("database", {})),
        logging=_build_dataclass(LoggingConfig, yaml_data.get("logging", {})),
        discord=DiscordConfig(
            bot_token=os.getenv("DISCORD_BOT_TOKEN", ""),
            channel_id=yaml_data.get("discord", {}).get("channel_id", ""),
            source_bot_name=yaml_data.get("discord", {}).get("source_bot_name", "Potion Perps"),
        ),
    )

    _validate(config)

    logger.info(
        "Config loaded: network=%s, preset=%s, auto_execute=%s, max_leverage=%d",
        config.exchange.network,
        config.strategy.active_preset,
        config.strategy.auto_execute,
        config.strategy.max_leverage,
    )

    return config


def _build_dataclass(cls, data: dict):
    """Build a dataclass from a dict, ignoring unknown keys."""
    valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
    filtered = {k: v for k, v in data.items() if k in valid_fields}
    return cls(**filtered)


def _validate(config: Config) -> None:
    """Validate config values. Raises ConfigError on problems."""
    errors = []

    # Exchange credentials
    if not config.exchange.account_address:
        errors.append("HL_ACCOUNT_ADDRESS not set in .env")
    if not config.exchange.api_secret:
        errors.append("HL_API_SECRET not set in .env")
    if config.exchange.network not in ("testnet", "mainnet"):
        errors.append(f"exchange.network must be 'testnet' or 'mainnet', got '{config.exchange.network}'")

    # Strategy — validate the active preset exists and its params
    try:
        preset = config.get_active_preset()
        tp = preset.tp_split
        if len(tp) != 3 or abs(sum(tp) - 1.0) > 0.01:
            errors.append(f"Active preset tp_split must have 3 values summing to 1.0, got {tp}")
        if preset.move_sl_to_breakeven_after not in ("tp1", "tp2", "never"):
            errors.append(
                f"move_sl_to_breakeven_after must be 'tp1', 'tp2', or 'never', "
                f"got '{preset.move_sl_to_breakeven_after}'"
            )
        if preset.size_pct <= 0:
            errors.append(f"Preset size_pct must be > 0, got {preset.size_pct}")
    except ConfigError as e:
        errors.append(str(e))

    if config.strategy.max_leverage < 1:
        errors.append(f"strategy.max_leverage must be >= 1, got {config.strategy.max_leverage}")

    # Validate all user-defined presets too
    for name, preset in config.strategy.presets.items():
        tp = preset.tp_split
        if len(tp) != 3 or abs(sum(tp) - 1.0) > 0.01:
            errors.append(f"Preset '{name}' tp_split must have 3 values summing to 1.0, got {tp}")
        if preset.move_sl_to_breakeven_after not in ("tp1", "tp2", "never"):
            errors.append(f"Preset '{name}' move_sl_to_breakeven_after invalid: '{preset.move_sl_to_breakeven_after}'")

    # Risk
    if config.risk.max_open_positions < 1:
        errors.append(f"risk.max_open_positions must be >= 1, got {config.risk.max_open_positions}")
    if config.risk.max_daily_loss_pct <= 0:
        errors.append(f"risk.max_daily_loss_pct must be > 0, got {config.risk.max_daily_loss_pct}")

    if errors:
        raise ConfigError("Config validation failed:\n  " + "\n  ".join(errors))
