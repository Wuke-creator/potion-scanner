"""Per-user config and credential storage.

Manages four tables (users, user_credentials, user_config, invite_codes) in
the same SQLite database as TradeDatabase. Credentials are Fernet-encrypted
at rest.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
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
    return datetime.now(timezone.utc).isoformat()


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
    telegram_chat_id       INTEGER,
    invite_code            TEXT,
    access_expires_at      TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
"""

_TELEGRAM_ADMINS_DDL = """\
CREATE TABLE IF NOT EXISTS telegram_admins (
    telegram_id   INTEGER PRIMARY KEY,
    added_by      INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);
"""

_INVITE_CODES_DDL = """\
CREATE TABLE IF NOT EXISTS invite_codes (
    code            TEXT PRIMARY KEY,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    duration_days   INTEGER,
    redeemed_by     TEXT,
    redeemed_at     TEXT,
    expires_at      TEXT,
    status          TEXT NOT NULL DEFAULT 'active'
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
            self._conn.execute(_INVITE_CODES_DDL)
            self._conn.execute(_TELEGRAM_ADMINS_DDL)
            self._migrate_user_config()

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
            "invite_code": row["invite_code"],
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

    # ------------------------------------------------------------------
    # Migrations
    # ------------------------------------------------------------------

    def _migrate_user_config(self) -> None:
        """Add new columns to user_config if they don't exist (for existing DBs)."""
        cursor = self._conn.execute("PRAGMA table_info(user_config)")
        existing = {row[1] for row in cursor.fetchall()}
        migrations = {
            "telegram_chat_id": "ALTER TABLE user_config ADD COLUMN telegram_chat_id INTEGER",
            "invite_code": "ALTER TABLE user_config ADD COLUMN invite_code TEXT",
            "access_expires_at": "ALTER TABLE user_config ADD COLUMN access_expires_at TEXT",
        }
        for col, sql in migrations.items():
            if col not in existing:
                self._conn.execute(sql)
                logger.info("Migrated user_config: added column %s", col)

    # ------------------------------------------------------------------
    # Telegram chat ID
    # ------------------------------------------------------------------

    def set_telegram_chat_id(self, user_id: str, chat_id: int) -> None:
        """Store the Telegram chat ID for a user."""
        now = _now()
        with self._conn:
            self._conn.execute(
                "UPDATE user_config SET telegram_chat_id = ?, updated_at = ? WHERE user_id = ?",
                (chat_id, now, user_id),
            )

    def get_telegram_chat_id(self, user_id: str) -> int | None:
        """Get the Telegram chat ID for a user."""
        row = self._conn.execute(
            "SELECT telegram_chat_id FROM user_config WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row or row["telegram_chat_id"] is None:
            return None
        return row["telegram_chat_id"]

    def get_user_by_telegram_chat_id(self, chat_id: int) -> str | None:
        """Look up user_id by Telegram chat ID. Returns None if not found."""
        row = self._conn.execute(
            "SELECT user_id FROM user_config WHERE telegram_chat_id = ?", (chat_id,)
        ).fetchone()
        return row["user_id"] if row else None

    def get_all_telegram_chat_ids(self) -> list[int]:
        """Get all Telegram chat IDs for active users."""
        rows = self._conn.execute(
            "SELECT uc.telegram_chat_id FROM user_config uc "
            "JOIN users u ON u.user_id = uc.user_id "
            "WHERE u.status = 'active' AND uc.telegram_chat_id IS NOT NULL"
        ).fetchall()
        return [row["telegram_chat_id"] for row in rows]

    # ------------------------------------------------------------------
    # Invite codes
    # ------------------------------------------------------------------

    def create_invite_code(
        self, code: str, created_by: str, duration_days: int | None = None
    ) -> dict[str, Any]:
        """Store a new invite code.

        Args:
            code: The invite code string (e.g. PPB-XXXX-XXXX).
            created_by: Admin identifier who generated the code.
            duration_days: Number of days the code grants access. None = unlimited.

        Returns:
            Dict with the code record fields.
        """
        now = _now()
        with self._conn:
            self._conn.execute(
                "INSERT INTO invite_codes (code, created_by, created_at, duration_days, status) "
                "VALUES (?, ?, ?, ?, 'active')",
                (code, created_by, now, duration_days),
            )
        logger.info("Created invite code %s (duration=%s days)", code, duration_days)
        return {
            "code": code,
            "created_by": created_by,
            "created_at": now,
            "duration_days": duration_days,
            "status": "active",
        }

    def validate_invite_code(self, code: str) -> dict[str, Any]:
        """Validate an invite code.

        Returns:
            Dict with 'valid' (bool) and 'reason' (str) keys.
            If valid, also includes the code record.
        """
        row = self._conn.execute(
            "SELECT * FROM invite_codes WHERE code = ?", (code,)
        ).fetchone()

        if not row:
            return {"valid": False, "reason": "Code not found"}

        if row["status"] == "redeemed":
            return {"valid": False, "reason": "Code already redeemed"}

        if row["status"] == "revoked":
            return {"valid": False, "reason": "Code has been revoked"}

        if row["status"] == "expired":
            return {"valid": False, "reason": "Code has expired"}

        return {
            "valid": True,
            "reason": "Valid",
            "code": row["code"],
            "duration_days": row["duration_days"],
            "created_by": row["created_by"],
        }

    def redeem_invite_code(self, code: str, user_id: str) -> str | None:
        """Redeem an invite code for a user.

        Sets the code status to 'redeemed', calculates expiry, and stores
        the invite_code and access_expires_at on the user's config.

        Returns:
            The access_expires_at ISO string, or None if unlimited.
        """
        now = _now()
        row = self._conn.execute(
            "SELECT * FROM invite_codes WHERE code = ?", (code,)
        ).fetchone()
        if not row:
            raise ValueError(f"Invite code {code} not found")

        duration_days = row["duration_days"]
        expires_at = None
        if duration_days is not None:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(days=duration_days)
            ).isoformat()

        with self._conn:
            # Mark code as redeemed
            self._conn.execute(
                "UPDATE invite_codes SET redeemed_by = ?, redeemed_at = ?, "
                "expires_at = ?, status = 'redeemed' WHERE code = ?",
                (user_id, now, expires_at, code),
            )
            # Store on user config
            self._conn.execute(
                "UPDATE user_config SET invite_code = ?, access_expires_at = ?, "
                "updated_at = ? WHERE user_id = ?",
                (code, expires_at, now, user_id),
            )

        logger.info("Redeemed invite code %s for user %s (expires=%s)", code, user_id, expires_at)
        return expires_at

    def list_invite_codes(self, status: str | None = None) -> list[dict[str, Any]]:
        """List invite codes, optionally filtered by status."""
        if status:
            rows = self._conn.execute(
                "SELECT * FROM invite_codes WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM invite_codes ORDER BY created_at DESC"
            ).fetchall()

        return [
            {
                "code": r["code"],
                "created_by": r["created_by"],
                "created_at": r["created_at"],
                "duration_days": r["duration_days"],
                "redeemed_by": r["redeemed_by"],
                "redeemed_at": r["redeemed_at"],
                "expires_at": r["expires_at"],
                "status": r["status"],
            }
            for r in rows
        ]

    def revoke_invite_code(self, code: str) -> bool:
        """Revoke an unused invite code. Returns True if revoked, False if not found or already used."""
        row = self._conn.execute(
            "SELECT status FROM invite_codes WHERE code = ?", (code,)
        ).fetchone()

        if not row:
            return False
        if row["status"] != "active":
            return False

        with self._conn:
            self._conn.execute(
                "UPDATE invite_codes SET status = 'revoked' WHERE code = ?",
                (code,),
            )
        logger.info("Revoked invite code %s", code)
        return True

    # ------------------------------------------------------------------
    # Access expiry
    # ------------------------------------------------------------------

    def get_access_expiry(self, user_id: str) -> str | None:
        """Get access expiry timestamp for a user. None = unlimited."""
        row = self._conn.execute(
            "SELECT access_expires_at FROM user_config WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return None
        return row["access_expires_at"]

    def get_expired_users(self) -> list[str]:
        """Return user_ids whose access has expired."""
        now = _now()
        rows = self._conn.execute(
            "SELECT uc.user_id FROM user_config uc "
            "JOIN users u ON u.user_id = uc.user_id "
            "WHERE uc.access_expires_at IS NOT NULL "
            "AND uc.access_expires_at < ? "
            "AND u.status = 'active'",
            (now,),
        ).fetchall()
        return [row["user_id"] for row in rows]

    def get_users_expiring_within(self, hours: int) -> list[tuple[str, str]]:
        """Return (user_id, access_expires_at) for active users expiring within N hours.

        Only returns users whose expiry is in the future but within the window.
        """
        now_dt = datetime.now(timezone.utc)
        cutoff = (now_dt + timedelta(hours=hours)).isoformat()
        now = now_dt.isoformat()
        rows = self._conn.execute(
            "SELECT uc.user_id, uc.access_expires_at FROM user_config uc "
            "JOIN users u ON u.user_id = uc.user_id "
            "WHERE uc.access_expires_at IS NOT NULL "
            "AND uc.access_expires_at > ? "
            "AND uc.access_expires_at <= ? "
            "AND u.status = 'active'",
            (now, cutoff),
        ).fetchall()
        return [(row["user_id"], row["access_expires_at"]) for row in rows]

    def extend_user_access(self, user_id: str, days: int) -> str:
        """Extend a user's access by N days from current expiry (or from now if expired/unset).

        Returns:
            The new access_expires_at ISO string.
        """
        current_expiry = self.get_access_expiry(user_id)
        now_dt = datetime.now(timezone.utc)

        if current_expiry:
            base = datetime.fromisoformat(current_expiry)
            # If already expired, extend from now instead of the past
            if base < now_dt:
                base = now_dt
        else:
            base = now_dt

        new_expiry = (base + timedelta(days=days)).isoformat()
        now = _now()
        with self._conn:
            self._conn.execute(
                "UPDATE user_config SET access_expires_at = ?, updated_at = ? WHERE user_id = ?",
                (new_expiry, now, user_id),
            )
        logger.info("Extended access for user %s to %s", user_id, new_expiry)
        return new_expiry

    def revoke_user_access(self, user_id: str) -> None:
        """Revoke a user's access by setting expiry to now."""
        now = _now()
        with self._conn:
            self._conn.execute(
                "UPDATE user_config SET access_expires_at = ?, updated_at = ? WHERE user_id = ?",
                (now, now, user_id),
            )
        self.set_user_status(user_id, "inactive")
        logger.info("Revoked access for user %s", user_id)

    # ------------------------------------------------------------------
    # Telegram admin management
    # ------------------------------------------------------------------

    def add_telegram_admin(self, telegram_id: int, added_by: int) -> bool:
        """Add a Telegram admin. Returns False if already exists."""
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO telegram_admins (telegram_id, added_by, created_at) VALUES (?, ?, ?)",
                    (telegram_id, added_by, _now()),
                )
            logger.info("Added Telegram admin %d (by %d)", telegram_id, added_by)
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_telegram_admin(self, telegram_id: int) -> bool:
        """Remove a Telegram admin. Returns False if not found."""
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM telegram_admins WHERE telegram_id = ?", (telegram_id,)
            )
        if cursor.rowcount > 0:
            logger.info("Removed Telegram admin %d", telegram_id)
            return True
        return False

    def list_telegram_admins(self) -> list[int]:
        """Return all dynamically added Telegram admin IDs."""
        rows = self._conn.execute(
            "SELECT telegram_id FROM telegram_admins ORDER BY created_at"
        ).fetchall()
        return [row["telegram_id"] for row in rows]

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
