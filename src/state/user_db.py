"""Per-user config and credential storage.

Manages three tables (users, user_credentials, user_config) in the same
SQLite database as TradeDatabase. Credentials are Fernet-encrypted at rest.
"""

import json
import logging
import sqlite3
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config.settings import (
    Config,
    ExchangeConfig,
    RiskConfig,
    StrategyConfig,
    StrategyPreset,
)
from src.crypto import encrypt, decrypt

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.utcnow().isoformat()


_USERS_DDL = """\
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""

_USER_CREDENTIALS_DDL = """\
CREATE TABLE IF NOT EXISTS user_credentials (
    user_id             TEXT PRIMARY KEY REFERENCES users(user_id),
    account_address_enc TEXT NOT NULL,
    api_wallet_enc      TEXT NOT NULL,
    api_secret_enc      TEXT NOT NULL,
    network             TEXT NOT NULL DEFAULT 'testnet',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
"""

_USER_CONFIG_DDL = """\
CREATE TABLE IF NOT EXISTS user_config (
    user_id                TEXT PRIMARY KEY REFERENCES users(user_id),
    active_preset          TEXT NOT NULL DEFAULT 'runner',
    auto_execute           INTEGER NOT NULL DEFAULT 0,
    max_leverage           INTEGER NOT NULL DEFAULT 20,
    size_by_risk_json      TEXT NOT NULL DEFAULT '{"LOW":4.0,"MEDIUM":2.0,"HIGH":1.0}',
    custom_presets_json    TEXT NOT NULL DEFAULT '{}',
    max_open_positions     INTEGER NOT NULL DEFAULT 10,
    max_daily_loss_pct     REAL NOT NULL DEFAULT 10.0,
    max_position_size_usd  REAL NOT NULL DEFAULT 500.0,
    max_total_exposure_usd REAL NOT NULL DEFAULT 2000.0,
    min_order_usd          REAL NOT NULL DEFAULT 10.0,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
"""


@dataclass
class UserRecord:
    user_id: str
    display_name: str
    status: str
    created_at: str
    updated_at: str


class UserDatabase:
    """Manages user registration, encrypted credentials, and per-user config.

    Uses the same SQLite file as TradeDatabase but operates on separate tables.
    """

    def __init__(self, db_path: str | Path = "data/trades.db"):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")

        self._create_tables()
        logger.info("UserDatabase ready: path=%s", self._db_path)

    def _create_tables(self) -> None:
        with self._conn:
            self._conn.execute(_USERS_DDL)
            self._conn.execute(_USER_CREDENTIALS_DDL)
            self._conn.execute(_USER_CONFIG_DDL)

    # ------------------------------------------------------------------
    # User CRUD
    # ------------------------------------------------------------------

    def create_user(
        self,
        user_id: str,
        display_name: str,
        credentials: dict[str, str],
        config: dict[str, Any] | None = None,
    ) -> UserRecord:
        """Create a user with credentials and optional config overrides.

        Args:
            user_id: Unique identifier for the user.
            display_name: Human-readable name.
            credentials: Must contain 'account_address', 'api_wallet', 'api_secret'.
                         Optional: 'network' (default 'testnet').
            config: Optional dict of config overrides (keys match user_config columns).

        Returns:
            The created UserRecord.
        """
        now = _now()
        config = config or {}

        with self._conn:
            # Insert user
            self._conn.execute(
                "INSERT INTO users (user_id, display_name, status, created_at, updated_at) "
                "VALUES (?, ?, 'active', ?, ?)",
                (user_id, display_name, now, now),
            )

            # Insert encrypted credentials
            self._conn.execute(
                "INSERT INTO user_credentials "
                "(user_id, account_address_enc, api_wallet_enc, api_secret_enc, network, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    encrypt(credentials["account_address"]),
                    encrypt(credentials["api_wallet"]),
                    encrypt(credentials["api_secret"]),
                    credentials.get("network", "testnet"),
                    now,
                    now,
                ),
            )

            # Insert config (with defaults for missing keys)
            size_by_risk = config.get("size_by_risk", {"LOW": 4.0, "MEDIUM": 2.0, "HIGH": 1.0})
            custom_presets = config.get("custom_presets", {})
            self._conn.execute(
                "INSERT INTO user_config "
                "(user_id, active_preset, auto_execute, max_leverage, size_by_risk_json, "
                "custom_presets_json, max_open_positions, max_daily_loss_pct, "
                "max_position_size_usd, max_total_exposure_usd, min_order_usd, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    config.get("active_preset", "runner"),
                    int(config.get("auto_execute", False)),
                    config.get("max_leverage", 20),
                    json.dumps(size_by_risk),
                    json.dumps(custom_presets),
                    config.get("max_open_positions", 10),
                    config.get("max_daily_loss_pct", 10.0),
                    config.get("max_position_size_usd", 500.0),
                    config.get("max_total_exposure_usd", 2000.0),
                    config.get("min_order_usd", 10.0),
                    now,
                    now,
                ),
            )

        logger.info("Created user: %s (%s)", user_id, display_name)
        return UserRecord(
            user_id=user_id,
            display_name=display_name,
            status="active",
            created_at=now,
            updated_at=now,
        )

    def get_user(self, user_id: str) -> UserRecord | None:
        """Get a user by ID."""
        row = self._conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return None
        return UserRecord(
            user_id=row["user_id"],
            display_name=row["display_name"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_users(self, status: str | None = None) -> list[UserRecord]:
        """List users, optionally filtered by status."""
        if status:
            rows = self._conn.execute(
                "SELECT * FROM users WHERE status = ? ORDER BY created_at", (status,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM users ORDER BY created_at"
            ).fetchall()
        return [
            UserRecord(
                user_id=r["user_id"],
                display_name=r["display_name"],
                status=r["status"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def get_active_users(self) -> list[UserRecord]:
        """Return all users with status='active'."""
        return self.list_users(status="active")

    def set_user_status(self, user_id: str, status: str) -> None:
        """Set user status to 'active' or 'inactive'."""
        now = _now()
        with self._conn:
            self._conn.execute(
                "UPDATE users SET status = ?, updated_at = ? WHERE user_id = ?",
                (status, now, user_id),
            )
        logger.info("User %s status → %s", user_id, status)

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    def get_user_credentials_decrypted(self, user_id: str) -> dict[str, str] | None:
        """Decrypt and return user credentials."""
        row = self._conn.execute(
            "SELECT * FROM user_credentials WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "account_address": decrypt(row["account_address_enc"]),
            "api_wallet": decrypt(row["api_wallet_enc"]),
            "api_secret": decrypt(row["api_secret_enc"]),
            "network": row["network"],
        }

    def update_user_credentials(self, user_id: str, **kwargs: str) -> None:
        """Update specific credential fields. Values are encrypted before storage."""
        now = _now()
        updates = []
        params: list[Any] = []

        enc_fields = {"account_address": "account_address_enc",
                       "api_wallet": "api_wallet_enc",
                       "api_secret": "api_secret_enc"}

        for key, value in kwargs.items():
            if key in enc_fields:
                updates.append(f"{enc_fields[key]} = ?")
                params.append(encrypt(value))
            elif key == "network":
                updates.append("network = ?")
                params.append(value)

        if not updates:
            return

        updates.append("updated_at = ?")
        params.append(now)
        params.append(user_id)

        with self._conn:
            self._conn.execute(
                f"UPDATE user_credentials SET {', '.join(updates)} WHERE user_id = ?",
                params,
            )
        logger.info("Updated credentials for user %s", user_id)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def get_user_config(self, user_id: str) -> dict[str, Any] | None:
        """Return raw user config as a dict."""
        row = self._conn.execute(
            "SELECT * FROM user_config WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "active_preset": row["active_preset"],
            "auto_execute": bool(row["auto_execute"]),
            "max_leverage": row["max_leverage"],
            "size_by_risk": json.loads(row["size_by_risk_json"]),
            "custom_presets": json.loads(row["custom_presets_json"]),
            "max_open_positions": row["max_open_positions"],
            "max_daily_loss_pct": row["max_daily_loss_pct"],
            "max_position_size_usd": row["max_position_size_usd"],
            "max_total_exposure_usd": row["max_total_exposure_usd"],
            "min_order_usd": row["min_order_usd"],
        }

    def update_user_config(self, user_id: str, **kwargs: Any) -> None:
        """Update specific config fields."""
        now = _now()
        updates = []
        params: list[Any] = []

        # Map Python names to column names for JSON fields
        json_fields = {"size_by_risk": "size_by_risk_json", "custom_presets": "custom_presets_json"}
        direct_fields = {
            "active_preset", "auto_execute", "max_leverage",
            "max_open_positions", "max_daily_loss_pct",
            "max_position_size_usd", "max_total_exposure_usd", "min_order_usd",
        }

        for key, value in kwargs.items():
            if key in json_fields:
                updates.append(f"{json_fields[key]} = ?")
                params.append(json.dumps(value))
            elif key in direct_fields:
                if key == "auto_execute":
                    value = int(value)
                updates.append(f"{key} = ?")
                params.append(value)

        if not updates:
            return

        updates.append("updated_at = ?")
        params.append(now)
        params.append(user_id)

        with self._conn:
            self._conn.execute(
                f"UPDATE user_config SET {', '.join(updates)} WHERE user_id = ?",
                params,
            )
        logger.info("Updated config for user %s", user_id)

    def get_user_config_as_config(self, user_id: str, global_config: Config) -> Config:
        """Build a per-user Config by merging DB values into the global config.

        Overrides exchange credentials, strategy settings, and risk limits
        from the user's DB records. Keeps input, database, logging, health,
        and discord settings from the global config.
        """
        user_creds = self.get_user_credentials_decrypted(user_id)
        user_cfg = self.get_user_config(user_id)

        if not user_creds or not user_cfg:
            raise ValueError(f"User {user_id} not found or incomplete data")

        # Build custom presets
        custom_presets: dict[str, StrategyPreset] = {}
        for name, preset_data in user_cfg.get("custom_presets", {}).items():
            if isinstance(preset_data, dict):
                custom_presets[name] = StrategyPreset(
                    tp_split=preset_data.get("tp_split", [0.33, 0.33, 0.34]),
                    move_sl_to_breakeven_after=preset_data.get("move_sl_to_breakeven_after", "tp1"),
                    size_pct=preset_data.get("size_pct", 2.0),
                )

        return Config(
            exchange=ExchangeConfig(
                network=user_creds["network"],
                account_address=user_creds["account_address"],
                api_wallet=user_creds["api_wallet"],
                api_secret=user_creds["api_secret"],
            ),
            input=global_config.input,
            strategy=StrategyConfig(
                active_preset=user_cfg["active_preset"],
                auto_execute=user_cfg["auto_execute"],
                max_leverage=user_cfg["max_leverage"],
                size_by_risk=user_cfg["size_by_risk"],
                presets=custom_presets,
            ),
            risk=RiskConfig(
                max_open_positions=user_cfg["max_open_positions"],
                max_daily_loss_pct=user_cfg["max_daily_loss_pct"],
                max_position_size_usd=user_cfg["max_position_size_usd"],
                max_total_exposure_usd=user_cfg["max_total_exposure_usd"],
                min_order_usd=user_cfg["min_order_usd"],
            ),
            database=global_config.database,
            logging=global_config.logging,
            health=global_config.health,
            discord=global_config.discord,
        )

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
