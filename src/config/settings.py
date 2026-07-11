"""Config loader for the Potion Discord → Telegram broadcaster.

Two sources:
  1. .env — secrets (Discord/Telegram/Whop tokens, encryption keys)
  2. config/config.yaml — non-secret runtime values (logging, paths, intervals)

Channel routing is built from env vars + the YAML `channels` section so the
deployment can swap channel IDs without code changes.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when required config or secrets are missing."""


# Source-type identifiers used for ref-link routing
SOURCE_PERPS = "perps"
SOURCE_MEMECOIN = "memecoin"
# Mirror mode: pass the Discord message through to Telegram verbatim. No
# classification, no parsing, no formatting wrap, no ref link appended.
# Used for channels whose message format doesn't match the structured perp
# or memecoin templates (e.g. third-party alert bots posting rich embeds).
SOURCE_MIRROR = "mirror"


@dataclass
class ChannelRoute:
    """One Discord channel → one referral link bucket."""

    channel_id: int
    key: str            # stable slug used for subscription preferences (e.g. "perp_bot")
    name: str           # human-readable label, used in alert footer + settings UI
    source_type: str    # SOURCE_PERPS or SOURCE_MEMECOIN
    ref_link: str       # final URL pasted into the Telegram alert
    display_name: str = ""  # short label for /data; falls back to name


@dataclass
class DiscordConfig:
    bot_token: str = ""
    guild_id: int = 0
    channels: list[ChannelRoute] = field(default_factory=list)

    def channel_by_id(self, channel_id: int) -> ChannelRoute | None:
        for ch in self.channels:
            if ch.channel_id == channel_id:
                return ch
        return None

    def channel_by_key(self, key: str) -> ChannelRoute | None:
        for ch in self.channels:
            if ch.key == key:
                return ch
        return None

    def channel_ids(self) -> set[int]:
        return {ch.channel_id for ch in self.channels}

    def channel_keys(self) -> list[str]:
        return [ch.key for ch in self.channels]


@dataclass
class TelegramConfig:
    """Telegram bot settings.

    DM-based architecture: the bot DMs each verified user individually.
    There is no shared group — per-user subscription preferences are stored
    in the verification DB and enforced by the Dispatcher.
    """

    bot_token: str = ""


@dataclass
class DispatcherConfig:
    """Rate-limit tuning for the DM fan-out dispatcher."""

    rate_per_sec: float = 25.0           # Telegram global bot limit is ~30/s
    max_concurrent: int = 25             # worker pool size
    per_send_timeout_sec: float = 15.0
    queue_max_size: int = 10000          # backpressure cap for incoming alerts


@dataclass
class DiscordOAuthConfig:
    """Discord OAuth2 credentials + Elite role gate for verification.

    The Discord application is the SAME one that owns the bot token (the
    Potion Scanner application). Its Client ID is the application/bot ID;
    its Client Secret is generated under OAuth2 → Reset Secret.

    The verification flow asks Discord for ``identify + guilds.members.read``
    then calls ``GET /users/@me/guilds/{guild_id}/member`` to read the
    user's roles in the Potion server. Access is granted if
    ``elite_role_id`` appears in that roles list.
    """

    client_id: str = ""
    client_secret: str = ""
    elite_role_id: str = ""                # Discord role ID for the Elite tier
    elite_signup_url: str = ""             # URL shown to non-Elite users in the denial DM
    api_base: str = "https://discord.com/api"
    authorize_url: str = "https://discord.com/api/oauth2/authorize"
    token_url: str = "https://discord.com/api/oauth2/token"
    scope: str = "identify email guilds.members.read"


@dataclass
class OAuthConfig:
    redirect_uri: str = ""
    host: str = "0.0.0.0"
    port: int = 8080
    state_secret: str = ""                 # HMAC key for signed state token
    refresh_token_encryption_key: str = ""  # Fernet key for refresh tokens


@dataclass
class VerificationConfig:
    db_path: str = "data/verified.db"
    pending_ttl_seconds: int = 600         # 10 min for pending OAuth state
    reverify_interval_seconds: int = 86400  # 24h
    reverify_sleep_between_users_ms: int = 500


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/bot.log"
    format: str = "json"                    # "json" (server) or "console" (dev)


@dataclass
class EmailBotConfig:
    """Email bot (win-back / re-engagement sequences).

    Uses Resend for delivery. The Whop webhook is signature-verified.
    """

    enabled: bool = False
    resend_api_key: str = ""
    resend_from_address: str = "Potion <team@potion.gg>"
    whop_webhook_secret: str = ""
    admin_webhook_secret: str = ""
    resend_webhook_secret: str = ""
    rejoin_url: str = "https://whop.com/joined/potion-alpha/"
    db_path: str = "data/email.db"
    email_events_db_path: str = "data/email_events.db"
    worker_poll_sec: float = 60.0
    worker_max_per_cycle: int = 50
    discord_admin_user_ids: list[int] = field(default_factory=list)


@dataclass
class AutomationsConfig:
    """Retention automations (Features 1-4 + shared activity tracker)."""

    enabled: bool = False
    activity_db_path: str = "data/activity.db"
    # Channel IDs to record message posts from. Bot needs View Channel +
    # Read Message History on each. Empty = features 2 and 4 no-op.
    activity_tracking_channel_ids: list[int] = field(default_factory=list)

    # Feature 2: inactivity detector
    inactivity_threshold_days: int = 14
    inactivity_detector_interval_hours: int = 24

    # Feature 3: monthly value reminder (Telegram DM)
    value_reminder_cycle_days: int = 30
    value_reminder_poll_interval_hours: int = 1

    # Feature 4: channel-level feeler email
    # Map channel_id -> variant key ("telegram_bot" | "tools" | "concierge")
    # so the right Drive Task 19 copy is rendered for each underused channel.
    feeler_channel_variants: dict[int, str] = field(default_factory=dict)
    feeler_low_engagement_threshold: int = 5  # unique posters in window
    feeler_window_days: int = 14
    feeler_cooldown_days: int = 30
    feeler_detector_interval_hours: int = 24

    # Feature 1: feature launch
    launch_cta_url: str = "https://whop.com/joined/potion-alpha/"

    # Whop API (for email lookup by discord_user_id)
    whop_api_key: str = ""
    whop_api_base: str = "https://api.whop.com"
    whop_company_id: str = ""
    whop_members_db_path: str = "data/whop_members.db"
    email_sync_on_startup: bool = True
    email_sync_interval_hours: int = 24

    # Whop reviews scanner (relays new reviews into a Discord staff channel)
    whop_reviews_db_path: str = "data/whop_reviews.db"
    whop_reviews_channel_id: int = 0  # 0 disables the feature
    whop_reviews_interval_seconds: int = 900
    whop_reviews_ping_on_low_stars: bool = False

    # Cancel survey DM: when a member loses the Elite role, DM them the
    # exit feedback survey link. Skipped if either field is empty.
    cancel_survey_url: str = ""  # CANCEL_SURVEY_URL env var
    cancel_survey_db_path: str = "data/cancel_survey_dms.db"
    cancel_survey_cooldown_seconds: int = 7 * 24 * 60 * 60  # 7 days

    # Whop promo code generator (separate key from WHOP_API_KEY, scoped to
    # promo_code:create + access_pass:basic:read). Bot mints a unique
    # stock=1 code per cancelling member so leaked codes die after one use.
    # Leave blank to disable per-user codes and fall back to the
    # hardcoded OFFERS table in the frontend.
    whop_promo_api_key: str = ""  # WHOP_PROMO_API_KEY env var
    cancel_survey_promo_ttl_days: int = 30
    # Whop access-pass / product id for the Bronze tier. When set, a
    # membership.activated for this pass enrols the member in the
    # Bronze -> Elite upsell sequence. Empty = dormant (no enrolment).
    whop_bronze_access_pass_id: str = ""  # WHOP_BRONZE_ACCESS_PASS_ID env var
    bronze_promo_ttl_days: int = 14       # day-5 code redemption window
    # Sync-driven Bronze upsell (Option 2). The free-tier Whop product id
    # ("Free Discord" = prod_LVAuYCd2uhi7y). go_live_at is the join-time
    # cutoff: only members whose free membership was created at/after this
    # epoch are enrolled, so the existing free backlog is never blasted.
    # Both unset/0 => dormant.
    whop_bronze_free_product_id: str = ""  # WHOP_BRONZE_FREE_PRODUCT_ID
    bronze_enroll_go_live_at_epoch: int = 0  # BRONZE_ENROLL_GO_LIVE_AT_EPOCH

    # AUT-033 Post-Retention Follow-Up Survey URL. Sent 7 days after a
    # cancelled member reactivates (the "what convinced you to stay?"
    # survey). Leave blank to disable scheduling — the email won't fire
    # without a real URL since the CTA would dead-end.
    post_retention_survey_url: str = ""  # POST_RETENTION_SURVEY_URL env var
    post_retention_delay_days: int = 7


@dataclass
class OpsCaptureConfig:
    """Ops dashboard capture: tickets / leadership / staff activity.

    Listens to the Potion #general, #alpha, and the need-support forum
    (whose threads are tickets) and persists every non-bot message to
    ops.db. Empty channel/forum IDs disable the listener.

    Senior staff IDs are used for two things: leadership @mention
    detection (we log a row whenever any of these IDs gets pinged in a
    captured channel) and staff activity tracking (we increment a daily
    bucket whenever any of these IDs posts a message).
    """

    enabled: bool = False
    db_path: str = "data/ops.db"
    general_channel_id: int = 0
    alpha_channel_id: int = 0
    ticket_forum_id: int = 0
    senior_staff_ids: list[str] = field(default_factory=list)


@dataclass
class TradingConfig:
    """Ostium Builder SDK 1-Tap Trade subsystem.

    Disabled by default. When ``enabled=False`` the /connect command
    short-circuits and the Quick Trade callback responds with a "not
    available" message, so the bot can ship and stage the code without
    activating the trading path.
    """

    enabled: bool = False
    executor_base_url: str = ""
    executor_secret: str = ""
    delegates_db_path: str = "data/trading_delegates.db"
    user_settings_db_path: str = "data/trading_user_settings.db"
    builder_address: str = ""        # 0x... receiver of fee bps; blank = no fee accrual
    builder_fee_bps: int = 0         # 0..50; 0 = no fee charged on opens
    max_collateral_usdc: float = 5000.0  # safety cap on single-trade size
    executor_timeout_sec: float = 30.0


@dataclass
class AutotradeConfig:
    """Signal-driven autotrade on Hyperliquid (dark by default).

    When a "Perp Bot Calls" signal is recorded, fire a perp trade for each
    allowlisted, connected, opted-in user, sized as a percent of their
    withdrawable USDC. Dark + dry-run + testnet by default so the whole
    path can be validated before any real order is placed.

    Safe go-live progression (all via Railway env):
      1. (testnet, dry_run)  -- default; nothing real, testnet reads
      2. (mainnet, dry_run)  -- real balances/sizing shown in DMs, places nothing
      3. (mainnet, live)     -- set AUTOTRADE_DRY_RUN=false

    Credentials reuse the trading DelegatesDB (master address + agent key);
    only allowlisted users can enable it, and each must also run /autotrade
    and accept the disclosure.
    """

    enabled: bool = False              # master switch / kill switch
    dry_run: bool = True               # log + DM the intended trade, place nothing
    venue: str = "hyperliquid"         # "hyperliquid" | "blofin"
    network: str = "testnet"           # HL: testnet|mainnet; Blofin: demo|prod (mainnet->prod)
    allowlist: frozenset[int] = field(default_factory=frozenset)
    source_channel_key: str = "perp_bot"   # only this channel auto-fires
    prefs_db_path: str = "data/autotrade_prefs.db"
    blofin_creds_db_path: str = "data/blofin_creds.db"
    default_size_pct: float = 5.0      # % of available balance per trade
    max_leverage: int = 20             # caps whatever the signal says
    max_per_day: int = 10              # per-user daily trade cap
    min_collateral_usdc: float = 5.0   # skip if computed size below this
    slippage_bps: int = 100            # IOC crossing slippage for market-in
    # Scale-out weighting across TP1/TP2/TP3 (auto-normalised, front-loaded by
    # default so the bulk is banked at the nearest target). AUTOTRADE_TP_SPLIT
    # accepts "50/30/20" or "0.5,0.3,0.2".
    tp_split_weights: tuple[float, ...] = (0.5, 0.3, 0.2)
    # Confirm-gated copying of discretionary human calls (cabal-chat).
    # Unlike source_channel_key (machine format, auto-fires), the copy
    # channel's posts parse tolerantly and NEVER fire without the user's
    # /autotrade copy confirm. Authors filter matches Discord user id OR
    # username; empty = any non-bot author.
    copy_channel_key: str = "cabal"
    copy_authors: frozenset[str] = field(default_factory=frozenset)
    copy_default_leverage: int = 5   # when the call says "low lev" w/o a number
    # Account-level risk guard (passivbot-derived; src/trading/autotrade_risk.py).
    # Only runs when risk_enabled AND the venue can supply an account snapshot
    # (Hyperliquid yes, Blofin not yet). Wallet exposure = notional / account
    # value; 0 disables an individual check.
    risk_enabled: bool = True            # master switch for the whole guard
    risk_symbol_we_limit: float = 1.0   # max exposure per coin (1.0 = 100% of account)
    risk_total_we_limit: float = 2.0    # max exposure across all open positions
    risk_max_drawdown_pct: float = 15.0  # block new entries this far below peak balance
    risk_no_stacking: bool = True       # one open position per coin


@dataclass
class WalletCopyConfig:
    """Hyperliquid wallet scout + copy watcher (dark by default).

    Two independent flags:

      WALLET_SCOUT_ENABLED  nightly leaderboard screen + trade-level
                            verification + tracked-set hysteresis + DM digest
      WALLET_WATCH_ENABLED  10-20s clearinghouseState poller over the tracked
                            set; copyable opens become confirm-gated
                            engine.propose_copy proposals, exits DM instantly

    Nothing fires without /autotrade copy confirm. The watcher needs the
    autotrade stack (AUTOTRADE_ENABLED) for proposals; the scout runs alone.
    Both read only public Hyperliquid data; no keys involved.
    """

    scout_enabled: bool = False
    watch_enabled: bool = False
    db_path: str = "data/wallet_scout.db"
    poll_sec: float = 15.0             # watcher cadence (10-20s sensible)
    scout_hour_utc: int = 2            # nightly scout run (UTC hour)
    max_tracked: int = 5               # tracked-set size cap
    max_finalists: int = 20            # screened wallets verified per night
    # screen thresholds (from the discovery pass that found 495 candidates)
    min_account_value: float = 30_000.0
    max_account_value: float = 20_000_000.0
    max_volume_ratio: float = 150.0    # allTime volume / account, MM filter
    alltime_month_factor: float = 1.5  # allTime pnl >= factor * month pnl
    # verification / scoring
    min_episodes: int = 5              # closed round-trips needed to score
    scalper_fills_per_day: float = 25.0
    dormant_hours: float = 96.0
    # hysteresis (streaks are consecutive nightly runs)
    promote_score: float = 60.0
    promote_streak: int = 2
    demote_score: float = 45.0
    demote_streak: int = 3
    swap_margin: float = 10.0          # candidate must beat worst tracked by this
    # watcher gates
    conviction_floor: float = 0.05     # their margin/equity below this = skip
    proposal_cooldown_min: float = 30.0
    # derived protective levels (their stops are invisible)
    atr_period: int = 14
    atr_interval: str = "1h"
    atr_mult: float = 1.5
    # reserved: auto-reduce when the wallet exits. NOT implemented; exits
    # are DM-only until Luke explicitly asks for the feature.
    mirror_exits: bool = False
    # first-run seed: the manually vetted wallets, tracked from day one
    seed_addresses: tuple[str, ...] = (
        "0xadd12adbbd5db87674b38af99b6dd34dd2a45e0d",
        "0x1f7b0d0c259f599536037b9c6c782c04a2aec71d",
        "0x9cbf099ff424979439dfba03f00b5961784c06ce",
    )


@dataclass
class BacktestConfig:
    """Backtesting layer for the wallet copy stack (dark by default).

    BACKTEST_SNAPSHOT_ENABLED starts the nightly archiver (leaderboard
    JSON + candles + funding + wallet states into data/backtest_cache.db).
    Data-only: no DMs, no trades. Meant to be flipped ON immediately, since
    Hyperliquid keeps only 5000 candles per interval and no leaderboard
    history: whatever isn't archived nightly is permanently gone.
    """

    snapshot_enabled: bool = False
    snapshot_hour_utc: int = 3          # after the 02:00 scout
    cache_db_path: str = "data/backtest_cache.db"
    max_snapshot_coins: int = 40
    # retention (days)
    candle_1m_keep_days: int = 90
    candle_15m_keep_days: int = 400
    fills_keep_days: int = 180
    # /backtest job surface (Phase 2)
    job_timeout_min: int = 30
    # simulation defaults (Phase 1/2): confirm-delay grid incl. a 15s
    # bot-speed rung for the hop-mode experiment; costs per side.
    delay_grid_sec: tuple[int, ...] = (15, 0, 120, 300, 900)
    taker_fee_bps: float = 6.0
    slippage_bps: float = 10.0


@dataclass
class ImageArchiveConfig:
    """Permanent image archive on Telegram's CDN.

    When ``enabled`` is True (i.e. ``archive_chat_id`` is set), every
    signal-attached Discord image is downloaded once and uploaded to
    the archive chat, yielding a permanent Telegram ``file_id`` saved
    on the open_signals row. Fan-out and lifecycle re-sends reuse the
    file_id, which doesn't expire (unlike Discord CDN URLs).

    Recommended setup: create a private Telegram channel called
    "Potion Signal Archive", add the bot as admin, set
    IMAGE_ARCHIVE_CHAT_ID to the channel id.
    """

    archive_chat_id: int = 0      # 0 disables (URL passthrough fallback)

    @property
    def enabled(self) -> bool:
        return self.archive_chat_id != 0


@dataclass
class TrackRecordConfig:
    """Public track-record channel poster.

    When ``channel_id`` is set, every terminal close on any monitored
    signal channel (the same set we forward to Telegram) posts a "what
    just happened" card to the configured Discord channel: title
    (WIN / LOSS / BREAKEVEN + pair + side + leverage), bullet list
    (entry, SL, TPs hit, source channel, closed timestamp), chart
    attachment, and a footer link back to the original alert. Wins and
    losses both post: the channel's value is being unfiltered.

    Idempotency is enforced via a small SQLite table (one row per
    signal_id) so the same close can't double-post if the lifecycle
    fires twice. Backfill on startup walks the last N days of closed
    signals once when ``backfill_on_startup`` is True; the bot remembers
    what it has already posted, so re-enabling it on restart is safe.
    """

    channel_id: int = 0           # 0 disables the whole feature
    db_path: str = "data/track_record.db"
    footer_url: str = ""          # link appended to each post; blank skips
    backfill_on_startup: bool = False
    backfill_days: int = 30
    backfill_pace_sec: float = 2.5

    @property
    def enabled(self) -> bool:
        return self.channel_id != 0


@dataclass
class Config:
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    dispatcher: DispatcherConfig = field(default_factory=DispatcherConfig)
    discord_oauth: DiscordOAuthConfig = field(default_factory=DiscordOAuthConfig)
    oauth: OAuthConfig = field(default_factory=OAuthConfig)
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    email_bot: EmailBotConfig = field(default_factory=EmailBotConfig)
    automations: AutomationsConfig = field(default_factory=AutomationsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    ops_capture: OpsCaptureConfig = field(default_factory=OpsCaptureConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    autotrade: AutotradeConfig = field(default_factory=AutotradeConfig)
    wallet_copy: WalletCopyConfig = field(default_factory=WalletCopyConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    image_archive: ImageArchiveConfig = field(
        default_factory=ImageArchiveConfig,
    )
    track_record: TrackRecordConfig = field(default_factory=TrackRecordConfig)


def _env_int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}")


def _build_channel_routes(yaml_channels: list[dict]) -> list[ChannelRoute]:
    """Resolve channel IDs and ref links from YAML + env vars."""
    routes: list[ChannelRoute] = []
    seen_keys: set[str] = set()
    for entry in yaml_channels or []:
        channel_id_env = entry.get("id_env")
        ref_link_env = entry.get("ref_link_env")
        key = entry.get("key", "")
        name = entry.get("name", "")
        source_type = entry.get("source_type", "")

        if not key:
            raise ConfigError(f"channel entry missing key: {entry!r}")
        if key in seen_keys:
            raise ConfigError(f"duplicate channel key: {key!r}")
        seen_keys.add(key)
        if not channel_id_env:
            raise ConfigError(f"channel {key!r} missing id_env")
        if not ref_link_env:
            raise ConfigError(f"channel {key!r} missing ref_link_env")
        if source_type not in (SOURCE_PERPS, SOURCE_MEMECOIN, SOURCE_MIRROR):
            raise ConfigError(
                f"channel {key!r} source_type must be {SOURCE_PERPS!r}, "
                f"{SOURCE_MEMECOIN!r}, or {SOURCE_MIRROR!r}, got {source_type!r}"
            )

        channel_id = _env_int(channel_id_env)
        if channel_id == 0:
            logger.warning(
                "Channel %r has no ID set in env (%s) — skipping", key, channel_id_env,
            )
            continue

        ref_link = os.getenv(ref_link_env, "").strip()
        if not ref_link:
            raise ConfigError(
                f"Channel {key!r} ref link missing — set {ref_link_env} in env"
            )

        routes.append(
            ChannelRoute(
                channel_id=channel_id,
                key=key,
                name=name or key,
                source_type=source_type,
                ref_link=ref_link,
                display_name=entry.get("display_name", "") or name or key,
            )
        )
    return routes


def load_config(
    config_path: str | Path = "config/config.yaml",
    env_file: str | Path = ".env",
) -> Config:
    """Load and validate the broadcaster config.

    Returns a fully populated Config. Raises ConfigError if any required
    field is missing or malformed.
    """
    env_path = Path(env_file)
    if env_path.exists():
        load_dotenv(env_path, override=True)
        logger.info("Loaded env from %s", env_path)
    else:
        logger.warning("No .env at %s — relying on process environment", env_path)

    yaml_data: dict = {}
    yaml_path = Path(config_path)
    if yaml_path.exists():
        with open(yaml_path) as f:
            yaml_data = yaml.safe_load(f) or {}
        logger.info("Loaded config from %s", yaml_path)
    else:
        logger.warning("No config file at %s — using defaults", yaml_path)

    discord_yaml = yaml_data.get("discord", {})
    discord_cfg = DiscordConfig(
        bot_token=os.getenv("DISCORD_BOT_TOKEN", ""),
        guild_id=_env_int("POTION_GUILD_ID"),
        channels=_build_channel_routes(discord_yaml.get("channels", [])),
    )

    telegram_cfg = TelegramConfig(
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
    )

    dispatcher_yaml = yaml_data.get("dispatcher", {})
    dispatcher_cfg = DispatcherConfig(
        rate_per_sec=float(
            os.getenv("DISPATCHER_RATE_PER_SEC", dispatcher_yaml.get("rate_per_sec", 25.0))
        ),
        max_concurrent=int(
            os.getenv("DISPATCHER_MAX_CONCURRENT", dispatcher_yaml.get("max_concurrent", 25))
        ),
        per_send_timeout_sec=float(
            dispatcher_yaml.get("per_send_timeout_sec", 15.0)
        ),
        queue_max_size=int(dispatcher_yaml.get("queue_max_size", 10000)),
    )

    discord_oauth_yaml = yaml_data.get("discord_oauth", {})
    discord_oauth_cfg = DiscordOAuthConfig(
        client_id=os.getenv("DISCORD_OAUTH_CLIENT_ID", ""),
        client_secret=os.getenv("DISCORD_OAUTH_CLIENT_SECRET", ""),
        elite_role_id=os.getenv("DISCORD_ELITE_ROLE_ID", ""),
        elite_signup_url=os.getenv(
            "ELITE_SIGNUP_URL",
            discord_oauth_yaml.get("elite_signup_url", "https://whop.com/joined/potion-alpha/"),
        ),
        api_base=discord_oauth_yaml.get("api_base", "https://discord.com/api"),
        authorize_url=discord_oauth_yaml.get(
            "authorize_url", "https://discord.com/api/oauth2/authorize",
        ),
        token_url=discord_oauth_yaml.get(
            "token_url", "https://discord.com/api/oauth2/token",
        ),
        scope=discord_oauth_yaml.get("scope", "identify email guilds.members.read"),
    )

    oauth_yaml = yaml_data.get("oauth", {})
    oauth_cfg = OAuthConfig(
        redirect_uri=os.getenv("OAUTH_REDIRECT_URI", ""),
        host=oauth_yaml.get("host", "0.0.0.0"),
        port=_env_int("OAUTH_PORT", oauth_yaml.get("port", 8080)),
        state_secret=os.getenv("OAUTH_STATE_SECRET", ""),
        refresh_token_encryption_key=os.getenv("WHOP_REFRESH_TOKEN_ENCRYPTION_KEY", ""),
    )

    verification_yaml = yaml_data.get("verification", {})
    verification_cfg = VerificationConfig(
        db_path=verification_yaml.get("db_path", "data/verified.db"),
        pending_ttl_seconds=verification_yaml.get("pending_ttl_seconds", 600),
        reverify_interval_seconds=verification_yaml.get("reverify_interval_seconds", 86400),
        reverify_sleep_between_users_ms=verification_yaml.get(
            "reverify_sleep_between_users_ms", 500
        ),
    )

    logging_yaml = yaml_data.get("logging", {})
    logging_cfg = LoggingConfig(
        level=os.getenv("LOG_LEVEL", logging_yaml.get("level", "INFO")),
        file=logging_yaml.get("file", "logs/bot.log"),
        format=logging_yaml.get("format", "json"),
    )

    email_yaml = yaml_data.get("email_bot", {})
    admin_ids_raw = os.getenv("DISCORD_ADMIN_USER_IDS", "").strip()
    admin_ids: list[int] = []
    if admin_ids_raw:
        for part in admin_ids_raw.split(","):
            part = part.strip()
            if part.isdigit():
                admin_ids.append(int(part))
    email_cfg = EmailBotConfig(
        enabled=bool(os.getenv("EMAIL_BOT_ENABLED", "").strip().lower() in ("1", "true", "yes")),
        resend_api_key=os.getenv("RESEND_API_KEY", ""),
        resend_from_address=os.getenv(
            "RESEND_FROM_ADDRESS",
            email_yaml.get("from_address", "Potion <team@potion.gg>"),
        ),
        whop_webhook_secret=os.getenv("WHOP_WEBHOOK_SECRET", ""),
        admin_webhook_secret=os.getenv("ADMIN_WEBHOOK_SECRET", ""),
        resend_webhook_secret=os.getenv("RESEND_WEBHOOK_SECRET", ""),
        rejoin_url=os.getenv(
            "POTION_REJOIN_URL",
            email_yaml.get("rejoin_url", "https://whop.com/joined/potion-alpha/"),
        ),
        db_path=email_yaml.get("db_path", "data/email.db"),
        email_events_db_path=os.getenv(
            "EMAIL_EVENTS_DB_PATH",
            email_yaml.get("email_events_db_path", "data/email_events.db"),
        ),
        worker_poll_sec=float(email_yaml.get("worker_poll_sec", 60)),
        worker_max_per_cycle=int(email_yaml.get("worker_max_per_cycle", 50)),
        discord_admin_user_ids=admin_ids,
    )

    automations_yaml = yaml_data.get("automations", {})
    activity_channel_ids_env = os.getenv("ACTIVITY_TRACKING_CHANNEL_IDS", "").strip()
    activity_channel_ids: list[int] = []
    if activity_channel_ids_env:
        for part in activity_channel_ids_env.split(","):
            part = part.strip()
            if part.isdigit():
                activity_channel_ids.append(int(part))
    else:
        activity_channel_ids = [
            int(x) for x in automations_yaml.get("activity_tracking_channel_ids", [])
            if str(x).isdigit()
        ]

    feeler_variants_raw = automations_yaml.get("feeler_channel_variants", {}) or {}
    feeler_variants: dict[int, str] = {}
    for k, v in feeler_variants_raw.items():
        try:
            feeler_variants[int(k)] = str(v)
        except (ValueError, TypeError):
            continue

    automations_cfg = AutomationsConfig(
        enabled=bool(os.getenv("AUTOMATIONS_ENABLED", "").strip().lower() in ("1", "true", "yes")),
        activity_db_path=automations_yaml.get("activity_db_path", "data/activity.db"),
        activity_tracking_channel_ids=activity_channel_ids,
        inactivity_threshold_days=int(automations_yaml.get("inactivity_threshold_days", 14)),
        inactivity_detector_interval_hours=int(automations_yaml.get("inactivity_detector_interval_hours", 24)),
        value_reminder_cycle_days=int(automations_yaml.get("value_reminder_cycle_days", 30)),
        value_reminder_poll_interval_hours=int(automations_yaml.get("value_reminder_poll_interval_hours", 1)),
        feeler_channel_variants=feeler_variants,
        feeler_low_engagement_threshold=int(automations_yaml.get("feeler_low_engagement_threshold", 5)),
        feeler_window_days=int(automations_yaml.get("feeler_window_days", 14)),
        feeler_cooldown_days=int(automations_yaml.get("feeler_cooldown_days", 30)),
        feeler_detector_interval_hours=int(automations_yaml.get("feeler_detector_interval_hours", 24)),
        launch_cta_url=os.getenv(
            "AUTOMATIONS_LAUNCH_CTA_URL",
            automations_yaml.get("launch_cta_url", "https://whop.com/joined/potion-alpha/"),
        ),
        whop_api_key=os.getenv("WHOP_API_KEY", ""),
        whop_api_base=automations_yaml.get("whop_api_base", "https://api.whop.com"),
        whop_company_id=os.getenv(
            "WHOP_COMPANY_ID",
            automations_yaml.get("whop_company_id", ""),
        ),
        whop_members_db_path=automations_yaml.get(
            "whop_members_db_path", "data/whop_members.db",
        ),
        email_sync_on_startup=bool(
            automations_yaml.get("email_sync_on_startup", True)
        ),
        email_sync_interval_hours=int(
            automations_yaml.get("email_sync_interval_hours", 24)
        ),
        whop_reviews_db_path=automations_yaml.get(
            "whop_reviews_db_path", "data/whop_reviews.db",
        ),
        whop_reviews_channel_id=_env_int(
            "WHOP_REVIEWS_CHANNEL_ID",
            int(automations_yaml.get("whop_reviews_channel_id", 0) or 0),
        ),
        whop_reviews_interval_seconds=int(
            automations_yaml.get("whop_reviews_interval_seconds", 900)
        ),
        whop_reviews_ping_on_low_stars=bool(
            automations_yaml.get("whop_reviews_ping_on_low_stars", False)
        ),
        cancel_survey_url=os.getenv(
            "CANCEL_SURVEY_URL",
            automations_yaml.get("cancel_survey_url", ""),
        ).strip(),
        cancel_survey_db_path=automations_yaml.get(
            "cancel_survey_db_path", "data/cancel_survey_dms.db",
        ),
        cancel_survey_cooldown_seconds=int(
            automations_yaml.get(
                "cancel_survey_cooldown_seconds", 7 * 24 * 60 * 60,
            )
        ),
        whop_promo_api_key=os.getenv(
            "WHOP_PROMO_API_KEY",
            automations_yaml.get("whop_promo_api_key", ""),
        ).strip(),
        cancel_survey_promo_ttl_days=int(
            automations_yaml.get("cancel_survey_promo_ttl_days", 30),
        ),
        whop_bronze_access_pass_id=os.getenv(
            "WHOP_BRONZE_ACCESS_PASS_ID",
            automations_yaml.get("whop_bronze_access_pass_id", ""),
        ).strip(),
        bronze_promo_ttl_days=int(
            automations_yaml.get("bronze_promo_ttl_days", 14),
        ),
        whop_bronze_free_product_id=os.getenv(
            "WHOP_BRONZE_FREE_PRODUCT_ID",
            automations_yaml.get("whop_bronze_free_product_id", ""),
        ).strip(),
        bronze_enroll_go_live_at_epoch=_env_int(
            "BRONZE_ENROLL_GO_LIVE_AT_EPOCH",
            int(automations_yaml.get("bronze_enroll_go_live_at_epoch", 0) or 0),
        ),
        post_retention_survey_url=os.getenv(
            "POST_RETENTION_SURVEY_URL",
            automations_yaml.get("post_retention_survey_url", ""),
        ).strip(),
        post_retention_delay_days=int(
            automations_yaml.get("post_retention_delay_days", 7),
        ),
    )

    ops_yaml = yaml_data.get("ops_capture", {})
    senior_ids_raw = os.getenv("POTION_SENIOR_STAFF_IDS", "").strip()
    senior_ids: list[str] = []
    if senior_ids_raw:
        for part in senior_ids_raw.split(","):
            part = part.strip()
            if part.isdigit():
                senior_ids.append(part)
    else:
        senior_ids = [
            str(x) for x in ops_yaml.get("senior_staff_ids", [])
            if str(x).strip().isdigit()
        ]
    ops_cfg = OpsCaptureConfig(
        enabled=bool(
            os.getenv("OPS_CAPTURE_ENABLED", "").strip().lower() in ("1", "true", "yes")
        ),
        db_path=ops_yaml.get("db_path", "data/ops.db"),
        general_channel_id=_env_int(
            "POTION_GENERAL_CHANNEL_ID",
            int(ops_yaml.get("general_channel_id", 0) or 0),
        ),
        alpha_channel_id=_env_int(
            "POTION_ALPHA_CHANNEL_ID",
            int(ops_yaml.get("alpha_channel_id", 0) or 0),
        ),
        ticket_forum_id=_env_int(
            "POTION_TICKET_FORUM_ID",
            int(ops_yaml.get("ticket_forum_id", 0) or 0),
        ),
        senior_staff_ids=senior_ids,
    )

    trading_yaml = yaml_data.get("trading", {})
    trading_cfg = TradingConfig(
        enabled=bool(
            os.getenv("TRADING_ENABLED", "").strip().lower() in ("1", "true", "yes")
        ),
        executor_base_url=os.getenv(
            "TRADE_EXECUTOR_BASE_URL",
            trading_yaml.get("executor_base_url", ""),
        ),
        executor_secret=os.getenv("TRADE_EXECUTOR_SECRET", ""),
        delegates_db_path=trading_yaml.get(
            "delegates_db_path", "data/trading_delegates.db",
        ),
        user_settings_db_path=trading_yaml.get(
            "user_settings_db_path", "data/trading_user_settings.db",
        ),
        builder_address=os.getenv("OSTIUM_BUILDER_ADDRESS", "").strip(),
        builder_fee_bps=_env_int("OSTIUM_BUILDER_FEE_BPS", 0),
        max_collateral_usdc=float(
            os.getenv(
                "TRADING_MAX_COLLATERAL_USDC",
                trading_yaml.get("max_collateral_usdc", 5000.0),
            )
        ),
        executor_timeout_sec=float(
            trading_yaml.get("executor_timeout_sec", 30.0)
        ),
    )

    autotrade_yaml = yaml_data.get("autotrade", {})
    _allow_raw = os.getenv("AUTOTRADE_ALLOWLIST", "").strip()
    _allow_ids: set[int] = set()
    for part in _allow_raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            _allow_ids.add(int(part))
        except ValueError:
            raise ConfigError(
                f"AUTOTRADE_ALLOWLIST entries must be integer Telegram IDs, got {part!r}"
            )
    _split_raw = os.getenv("AUTOTRADE_TP_SPLIT", "").strip()
    if _split_raw:
        _split_parts = [
            p for p in _split_raw.replace(",", "/").replace(" ", "/").split("/") if p
        ]
        try:
            _tp_weights = tuple(float(p) for p in _split_parts)
        except ValueError:
            raise ConfigError(
                f"AUTOTRADE_TP_SPLIT must be numbers like 50/30/20, got {_split_raw!r}"
            )
        if not _tp_weights or sum(_tp_weights) <= 0:
            raise ConfigError("AUTOTRADE_TP_SPLIT must have a positive sum")
    else:
        _tp_weights = (0.5, 0.3, 0.2)

    autotrade_cfg = AutotradeConfig(
        enabled=os.getenv("AUTOTRADE_ENABLED", "").strip().lower()
        in ("1", "true", "yes"),
        # Defaults ON: unset means dry-run. Only an explicit false disables it.
        dry_run=os.getenv("AUTOTRADE_DRY_RUN", "true").strip().lower()
        not in ("0", "false", "no"),
        venue=os.getenv(
            "AUTOTRADE_VENUE", autotrade_yaml.get("venue", "hyperliquid"),
        ).strip().lower(),
        network=os.getenv(
            "AUTOTRADE_NETWORK", autotrade_yaml.get("network", "testnet"),
        ).strip().lower(),
        allowlist=frozenset(_allow_ids),
        source_channel_key=os.getenv(
            "AUTOTRADE_SOURCE_KEY",
            autotrade_yaml.get("source_channel_key", "perp_bot"),
        ).strip(),
        prefs_db_path=autotrade_yaml.get(
            "prefs_db_path", "data/autotrade_prefs.db",
        ),
        blofin_creds_db_path=autotrade_yaml.get(
            "blofin_creds_db_path", "data/blofin_creds.db",
        ),
        default_size_pct=float(
            os.getenv(
                "AUTOTRADE_DEFAULT_PCT",
                autotrade_yaml.get("default_size_pct", 5.0),
            )
        ),
        max_leverage=_env_int(
            "AUTOTRADE_MAX_LEVERAGE",
            int(autotrade_yaml.get("max_leverage", 20)),
        ),
        max_per_day=_env_int(
            "AUTOTRADE_MAX_PER_DAY",
            int(autotrade_yaml.get("max_per_day", 10)),
        ),
        min_collateral_usdc=float(
            os.getenv(
                "AUTOTRADE_MIN_COLLATERAL_USDC",
                autotrade_yaml.get("min_collateral_usdc", 5.0),
            )
        ),
        slippage_bps=_env_int(
            "AUTOTRADE_SLIPPAGE_BPS",
            int(autotrade_yaml.get("slippage_bps", 100)),
        ),
        tp_split_weights=_tp_weights,
        copy_channel_key=os.getenv(
            "AUTOTRADE_COPY_CHANNEL_KEY",
            autotrade_yaml.get("copy_channel_key", "cabal"),
        ).strip(),
        copy_authors=frozenset(
            a.strip() for a in os.getenv("AUTOTRADE_COPY_AUTHORS", "").split(",")
            if a.strip()
        ),
        copy_default_leverage=_env_int(
            "AUTOTRADE_COPY_DEFAULT_LEVERAGE",
            int(autotrade_yaml.get("copy_default_leverage", 5)),
        ),
        risk_enabled=os.getenv(
            "AUTOTRADE_RISK_ENABLED",
            str(autotrade_yaml.get("risk_enabled", "true")),
        ).strip().lower() not in ("0", "false", "no"),
        risk_symbol_we_limit=float(
            os.getenv(
                "AUTOTRADE_RISK_SYMBOL_WE",
                autotrade_yaml.get("risk_symbol_we_limit", 1.0),
            )
        ),
        risk_total_we_limit=float(
            os.getenv(
                "AUTOTRADE_RISK_TOTAL_WE",
                autotrade_yaml.get("risk_total_we_limit", 2.0),
            )
        ),
        risk_max_drawdown_pct=float(
            os.getenv(
                "AUTOTRADE_RISK_MAX_DRAWDOWN_PCT",
                autotrade_yaml.get("risk_max_drawdown_pct", 15.0),
            )
        ),
        risk_no_stacking=os.getenv(
            "AUTOTRADE_RISK_NO_STACKING",
            str(autotrade_yaml.get("risk_no_stacking", "true")),
        ).strip().lower() != "false",
    )

    wallet_yaml = yaml_data.get("wallet_copy", {})
    _seed_raw = os.getenv("WALLET_SEED_ADDRESSES", "").strip()
    if _seed_raw:
        _seed = tuple(
            a.strip().lower() for a in _seed_raw.split(",") if a.strip()
        )
    else:
        _seed = WalletCopyConfig.seed_addresses
    wallet_copy_cfg = WalletCopyConfig(
        scout_enabled=os.getenv("WALLET_SCOUT_ENABLED", "").strip().lower()
        in ("1", "true", "yes"),
        watch_enabled=os.getenv("WALLET_WATCH_ENABLED", "").strip().lower()
        in ("1", "true", "yes"),
        db_path=wallet_yaml.get("db_path", "data/wallet_scout.db"),
        poll_sec=float(
            os.getenv("WALLET_POLL_SEC", wallet_yaml.get("poll_sec", 15.0))
        ),
        scout_hour_utc=_env_int(
            "WALLET_SCOUT_HOUR_UTC", int(wallet_yaml.get("scout_hour_utc", 2)),
        ),
        max_tracked=_env_int(
            "WALLET_MAX_TRACKED", int(wallet_yaml.get("max_tracked", 5)),
        ),
        max_finalists=int(wallet_yaml.get("max_finalists", 20)),
        min_account_value=float(wallet_yaml.get("min_account_value", 30_000.0)),
        max_account_value=float(wallet_yaml.get("max_account_value", 20_000_000.0)),
        max_volume_ratio=float(wallet_yaml.get("max_volume_ratio", 150.0)),
        alltime_month_factor=float(wallet_yaml.get("alltime_month_factor", 1.5)),
        min_episodes=int(wallet_yaml.get("min_episodes", 5)),
        scalper_fills_per_day=float(
            wallet_yaml.get("scalper_fills_per_day", 25.0)
        ),
        dormant_hours=float(wallet_yaml.get("dormant_hours", 96.0)),
        promote_score=float(wallet_yaml.get("promote_score", 60.0)),
        promote_streak=int(wallet_yaml.get("promote_streak", 2)),
        demote_score=float(wallet_yaml.get("demote_score", 45.0)),
        demote_streak=int(wallet_yaml.get("demote_streak", 3)),
        swap_margin=float(wallet_yaml.get("swap_margin", 10.0)),
        conviction_floor=float(
            os.getenv(
                "WALLET_CONVICTION_FLOOR",
                wallet_yaml.get("conviction_floor", 0.05),
            )
        ),
        proposal_cooldown_min=float(
            os.getenv(
                "WALLET_PROPOSAL_COOLDOWN_MIN",
                wallet_yaml.get("proposal_cooldown_min", 30.0),
            )
        ),
        atr_period=int(wallet_yaml.get("atr_period", 14)),
        atr_interval=str(wallet_yaml.get("atr_interval", "1h")),
        atr_mult=float(
            os.getenv("WALLET_ATR_MULT", wallet_yaml.get("atr_mult", 1.5))
        ),
        mirror_exits=False,   # reserved; intentionally not env-readable yet
        seed_addresses=_seed,
    )

    backtest_yaml = yaml_data.get("backtest", {})
    backtest_cfg = BacktestConfig(
        snapshot_enabled=os.getenv("BACKTEST_SNAPSHOT_ENABLED", "").strip().lower()
        in ("1", "true", "yes"),
        snapshot_hour_utc=_env_int(
            "BACKTEST_SNAPSHOT_HOUR_UTC",
            int(backtest_yaml.get("snapshot_hour_utc", 3)),
        ),
        cache_db_path=backtest_yaml.get("cache_db_path", "data/backtest_cache.db"),
        max_snapshot_coins=int(backtest_yaml.get("max_snapshot_coins", 40)),
        candle_1m_keep_days=int(backtest_yaml.get("candle_1m_keep_days", 90)),
        candle_15m_keep_days=int(backtest_yaml.get("candle_15m_keep_days", 400)),
        fills_keep_days=int(backtest_yaml.get("fills_keep_days", 180)),
        job_timeout_min=_env_int(
            "BACKTEST_JOB_TIMEOUT_MIN",
            int(backtest_yaml.get("job_timeout_min", 30)),
        ),
        taker_fee_bps=float(backtest_yaml.get("taker_fee_bps", 6.0)),
        slippage_bps=float(backtest_yaml.get("slippage_bps", 10.0)),
    )

    image_archive_cfg = ImageArchiveConfig(
        archive_chat_id=_env_int("IMAGE_ARCHIVE_CHAT_ID", 0),
    )

    track_record_yaml = yaml_data.get("track_record", {})
    track_record_cfg = TrackRecordConfig(
        channel_id=_env_int(
            "TRACK_RECORD_CHANNEL_ID",
            int(track_record_yaml.get("channel_id", 0) or 0),
        ),
        db_path=track_record_yaml.get("db_path", "data/track_record.db"),
        footer_url=os.getenv(
            "TRACK_RECORD_FOOTER_URL",
            track_record_yaml.get("footer_url", ""),
        ).strip(),
        backfill_on_startup=bool(
            os.getenv("TRACK_RECORD_BACKFILL_ON_STARTUP", "").strip().lower()
            in ("1", "true", "yes")
        ),
        backfill_days=int(track_record_yaml.get("backfill_days", 30)),
        backfill_pace_sec=float(
            track_record_yaml.get("backfill_pace_sec", 2.5)
        ),
    )

    config = Config(
        discord=discord_cfg,
        telegram=telegram_cfg,
        dispatcher=dispatcher_cfg,
        discord_oauth=discord_oauth_cfg,
        oauth=oauth_cfg,
        verification=verification_cfg,
        email_bot=email_cfg,
        automations=automations_cfg,
        logging=logging_cfg,
        ops_capture=ops_cfg,
        trading=trading_cfg,
        autotrade=autotrade_cfg,
        wallet_copy=wallet_copy_cfg,
        backtest=backtest_cfg,
        image_archive=image_archive_cfg,
        track_record=track_record_cfg,
    )

    _validate(config)
    logger.info(
        "Config loaded: %d channel(s), guild=%d, dispatcher rate=%.1f/s",
        len(config.discord.channels),
        config.discord.guild_id,
        config.dispatcher.rate_per_sec,
    )
    return config


def _validate(config: Config) -> None:
    errors: list[str] = []

    if not config.discord.bot_token:
        errors.append("DISCORD_BOT_TOKEN not set")
    if config.discord.guild_id == 0:
        errors.append("POTION_GUILD_ID not set or zero")
    if not config.discord.channels:
        errors.append("No Discord channels configured (check config.yaml + env vars)")

    if not config.telegram.bot_token:
        errors.append("TELEGRAM_BOT_TOKEN not set")

    if config.dispatcher.rate_per_sec <= 0:
        errors.append("DISPATCHER_RATE_PER_SEC must be > 0")
    if config.dispatcher.max_concurrent <= 0:
        errors.append("DISPATCHER_MAX_CONCURRENT must be > 0")

    if not config.discord_oauth.client_id:
        errors.append("DISCORD_OAUTH_CLIENT_ID not set")
    if not config.discord_oauth.client_secret:
        errors.append("DISCORD_OAUTH_CLIENT_SECRET not set")
    if not config.discord_oauth.elite_role_id:
        errors.append("DISCORD_ELITE_ROLE_ID not set")

    if not config.oauth.redirect_uri:
        errors.append("OAUTH_REDIRECT_URI not set")
    if not config.oauth.state_secret:
        errors.append("OAUTH_STATE_SECRET not set")
    if not config.oauth.refresh_token_encryption_key:
        errors.append("WHOP_REFRESH_TOKEN_ENCRYPTION_KEY not set")

    if config.trading.enabled:
        if not config.trading.executor_base_url:
            errors.append(
                "TRADE_EXECUTOR_BASE_URL must be set when TRADING_ENABLED=true"
            )
        if not config.trading.executor_secret:
            errors.append(
                "TRADE_EXECUTOR_SECRET must be set when TRADING_ENABLED=true"
            )
        if config.trading.builder_fee_bps < 0 or config.trading.builder_fee_bps > 50:
            errors.append("OSTIUM_BUILDER_FEE_BPS must be between 0 and 50")
        if (
            config.trading.builder_fee_bps > 0
            and not config.trading.builder_address
        ):
            errors.append(
                "OSTIUM_BUILDER_ADDRESS must be set when "
                "OSTIUM_BUILDER_FEE_BPS > 0"
            )

    if errors:
        raise ConfigError("Config validation failed:\n  " + "\n  ".join(errors))
