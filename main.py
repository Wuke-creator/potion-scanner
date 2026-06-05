"""Potion Discord → Telegram Signals Bot — entry point.

Wires together, in order:

  1. Verification subsystem (Whop OAuth + SQLite DB + OAuth callback
     server + Telegram command handlers + 24h reverify cron)
  2. Dispatcher (rate-limited DM fan-out to all active verified users)
  3. Router (classify + parse + format, hands each alert to dispatcher)
  4. Discord listener (multi-channel, pushes messages onto an async queue)
  5. Queue consumer that pulls from the Discord queue into the router

Single asyncio event loop, single process. Shutdown is graceful:
SIGINT/SIGTERM sets an event, everything drains and stops in reverse
order.
"""

from __future__ import annotations

import asyncio
import os
import logging
import signal

from telegram import Bot

from src.analytics import AnalyticsDB
from src.automations import ActivityDB
from src.automations.cancel_survey_dm import CancelSurveyDM
from src.automations.channel_feeler import ChannelFeeler
from src.automations.feature_launch import FeatureLaunchBroadcaster
from src.automations.inactivity_detector import InactivityDetector
from src.automations.inactivity_day10 import InactivityDay10Email
from src.automations.onboarding_sequence import OnboardingSequence
from src.automations.dunning_sequence import DunningSequence
from src.automations.pre_renewal_email import PreRenewalEmail
from src.automations.pre_pause_return_email import PrePauseReturnEmail
from src.automations.save_offer_router import SaveOfferRouter
from src.automations.value_reminder import ValueReminder
from src.automations.whop_email_sync import WhopEmailSync, WhopEmailSyncCron
from src.automations.whop_members_db import WhopMembersDB
from src.automations.whop_reviews_sync import WhopReviewsDB, WhopReviewsSync
from src.config import Config, load_config
from src.discord_listener import DiscordListener, IncomingMessage
from src.dispatcher import Dispatcher
from src.email_bot import EmailDB
from src.email_bot.branch_evaluator import EvalContext
from src.email_bot.discord_commands import EmailSlashCommands
from src.email_bot.analytics import EmailAnalytics
from src.email_bot.engagement_scoring import EngagementScoreDB
from src.email_bot.events_db import EmailEventsDB
from src.email_bot.protected_sender import ProtectedSender
from src.email_bot.sender import ResendClient
from src.email_bot.suppression_metrics import SuppressionMetrics
from src.email_bot.webhook import EmailWebhookHandlers
from src.email_bot.worker import EmailWorker
from src.router import Router
from src.utils.logger import setup_logging

logger = logging.getLogger(__name__)


async def _consume_queue(
    queue: asyncio.Queue[IncomingMessage],
    router: Router,
    shutdown: asyncio.Event,
) -> None:
    """Pull messages off the Discord queue and hand them to the router."""
    while not shutdown.is_set():
        try:
            message = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        await router.handle(message)


# ---------------------------------------------------------------------------
# branch_evaluator adapters
# ---------------------------------------------------------------------------
#
# The evaluator's Protocols (WhopMembersLookup, SubscribersLookup) accept any
# object exposing the right async methods. These thin wrappers point the
# protocol methods at the existing production DB handles without inventing a
# new lookup layer. Tests can pass their own duck-typed stubs unchanged.


class _WhopMembersAdapter:
    """WhopMembersLookup adapter over the production WhopMembersDB.

    is_paid_member: a paid Elite member has a valid=1 row in whop_members.
    first_seen_at:  the cached enrollment timestamp the DB stores.

    Both methods short-circuit to safe defaults when the underlying DB
    returns no row -- the branch evaluator already treats None as
    "fall through to default", so we don't need to invent sentinel
    values here.
    """

    def __init__(self, members_db) -> None:
        self._db = members_db

    async def is_paid_member(self, email: str) -> bool:
        # WhopMembersDB exposes list_valid_with_email() but not a single
        # email lookup. The evaluator only fires once per due send, so
        # the LOWER() scan is acceptable until it becomes a bottleneck.
        if not email:
            return False
        try:
            rows = await self._db.list_valid_with_email()
        except Exception:
            logger.exception(
                "WhopMembersAdapter.is_paid_member: list crashed for %s",
                email,
            )
            return False
        target = email.strip().lower()
        return any((r.email or "").strip().lower() == target for r in rows)

    async def first_seen_at(self, email: str) -> int | None:
        if not email:
            return None
        try:
            rows = await self._db.list_valid_with_email()
        except Exception:
            logger.exception(
                "WhopMembersAdapter.first_seen_at: list crashed for %s",
                email,
            )
            return None
        target = email.strip().lower()
        for r in rows:
            if (r.email or "").strip().lower() == target:
                return int(r.first_seen_at) if r.first_seen_at else None
        return None


class _SubscribersAdapter:
    """SubscribersLookup adapter over the production EmailDB.

    The subscribers table already stores exit_reason at upsert time
    (set by the Whop cancellation webhook). The winback branch routes
    on it via a small mapping in branch_evaluator. ``None`` is returned
    when no row exists, which the evaluator treats as "default".
    """

    def __init__(self, email_db) -> None:
        self._db = email_db

    async def exit_reason(self, email: str) -> str | None:
        if not email:
            return None
        try:
            sub = await self._db.get_subscriber(email.lower().strip())
        except Exception:
            logger.exception(
                "SubscribersAdapter.exit_reason: lookup crashed for %s",
                email,
            )
            return None
        if sub is None:
            return None
        return sub.exit_reason or None


async def run(config: Config) -> None:
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            # Windows: add_signal_handler isn't implemented on asyncio loops.
            # KeyboardInterrupt will still unwind the top-level asyncio.run().
            pass

    telegram_bot = Bot(token=config.telegram.bot_token)

    # --- Analytics: track signal counts + PnL events ---
    analytics = AnalyticsDB(db_path="data/analytics.db")
    await analytics.open()

    # --- Open-signals memory: enriches lifecycle events (TP1 hit etc.)
    # with the original signal's entry/SL/TP prices, so image-bot updates
    # like "WET Update: TP1 here" carry the actual numbers on Telegram.
    from src.automations.open_signals_db import OpenSignalsDB

    open_signals_db = OpenSignalsDB(db_path="data/open_signals.db")
    await open_signals_db.open()

    # --- Verification subsystem (commands read analytics for /data) ---
    from src.verification.runtime import build_verification_runtime

    verification = await build_verification_runtime(
        config=config, telegram_bot=telegram_bot, analytics=analytics,
    )

    # --- Dispatcher: fan out alerts as DMs to all active verified users ---
    dispatcher = Dispatcher(
        bot=telegram_bot,
        db=verification.db,
        config=config.dispatcher,
    )

    # --- Trading subsystem (Ostium Builder SDK 1-Tap Trade) ---
    # Disabled by default; flip TRADING_ENABLED=true on Railway after the
    # trade-executor sidecar service is deployed and reachable on the
    # private network. Until then the bot ships the code but does not
    # surface the 1-Tap button on signal alerts.
    delegates_db = None
    trading_settings_db = None
    trade_executor_client = None
    if config.trading.enabled:
        from src.trading.commands import TradingCommands
        from src.trading.delegates_db import DelegatesDB
        from src.trading.executor_client import TradeExecutorClient
        from src.trading.settings_ui import TradingSettingsUI
        from src.trading.trade_flow import TradeFlow
        from src.trading.user_settings_db import UserTradingSettingsDB

        delegates_db = DelegatesDB(db_path=config.trading.delegates_db_path)
        await delegates_db.open()
        trading_settings_db = UserTradingSettingsDB(
            db_path=config.trading.user_settings_db_path,
        )
        await trading_settings_db.open()
        trade_executor_client = TradeExecutorClient(
            base_url=config.trading.executor_base_url,
            shared_secret=config.trading.executor_secret,
            request_timeout_sec=config.trading.executor_timeout_sec,
        )
        await trade_executor_client.open()

        trading_commands = TradingCommands(
            config=config,
            verification_db=verification.db,
            delegates_db=delegates_db,
            settings_db=trading_settings_db,
        )
        trading_commands.register(verification.application)
        trade_flow = TradeFlow(
            config=config,
            verification_db=verification.db,
            delegates_db=delegates_db,
            settings_db=trading_settings_db,
            open_signals_db=open_signals_db,
            executor=trade_executor_client,
        )
        trade_flow.register(verification.application)
        trading_settings_ui = TradingSettingsUI(settings_db=trading_settings_db)
        trading_settings_ui.register(verification.application)
        logger.info(
            "Trading subsystem enabled: executor=%s builder_fee_bps=%d",
            config.trading.executor_base_url,
            config.trading.builder_fee_bps,
        )

    # --- Admin test-signal endpoint (always mounted when admin secret is set) ---
    # Lets an operator fire a synthetic signal at a single verified user
    # without spamming the entire subscriber base. Auth via X-Admin-Secret
    # header matching ADMIN_WEBHOOK_SECRET. Useful for smoke-testing the
    # 1-Tap Trade flow on a single account.
    if config.email_bot.admin_webhook_secret:
        from src.trading.admin_endpoint import AdminTradingEndpoint

        admin_trading_endpoint = AdminTradingEndpoint(
            config=config,
            admin_secret=config.email_bot.admin_webhook_secret,
            verification_db=verification.db,
            open_signals_db=open_signals_db,
            telegram_bot=telegram_bot,
        )
        admin_trading_endpoint.register(verification.callback_server.app)
        logger.info("Admin trading endpoint registered at /admin/trading/test-signal")

    # --- Image archive: Discord URL -> permanent Telegram file_id ---
    # When IMAGE_ARCHIVE_CHAT_ID is set, every signal-attached chart is
    # uploaded once to the archive chat and reused for fan-out + lifecycle
    # re-sends. When unset, image forwarding falls back to URL passthrough
    # (works for ~24h while Discord's CDN signature is valid).
    image_archive = None
    if config.image_archive.enabled:
        from src.automations.image_archive import ImageArchive

        image_archive = ImageArchive(
            bot=telegram_bot,
            archive_chat_id=config.image_archive.archive_chat_id,
            open_signals_db=open_signals_db,
        )
        logger.info(
            "Image archive enabled: chat_id=%d",
            config.image_archive.archive_chat_id,
        )
    else:
        logger.info(
            "Image archive disabled (set IMAGE_ARCHIVE_CHAT_ID to enable). "
            "Signal images will fall back to Discord-URL passthrough."
        )

    # --- Automations: shared activity tracker (feeds Features 2 + 4) ---
    activity_db: ActivityDB | None = None
    whop_members_db: WhopMembersDB | None = None
    if config.automations.enabled:
        activity_db = ActivityDB(db_path=config.automations.activity_db_path)
        await activity_db.open()
        # Whop member roster (full Elite audience for email features).
        # Opened regardless of whether the Whop API key is set; if it's not,
        # the table stays empty and email features fall back to the
        # Telegram-verified subset.
        whop_members_db = WhopMembersDB(
            db_path=config.automations.whop_members_db_path,
        )
        await whop_members_db.open()

    # Build the listener with (optionally) the activity hook attached
    async def _record_activity(discord_user_id: str, channel_id: int) -> None:
        if activity_db is None:
            return
        try:
            await activity_db.record_post(discord_user_id, channel_id)
        except Exception:
            logger.exception(
                "Failed to record activity for user=%s channel=%d",
                discord_user_id, channel_id,
            )

    # --- Ops capture: tickets / leadership / staff activity for the dashboard ---
    ops_db = None
    ops_capture = None
    ops_flat_channels: set[int] = set()
    ops_thread_parents: set[int] = set()
    if config.ops_capture.enabled:
        from src.ops import OpsCapture, OpsDB

        ops_db = OpsDB(db_path=config.ops_capture.db_path)
        await ops_db.open()
        ops_capture = OpsCapture(
            ops_db=ops_db,
            general_channel_id=config.ops_capture.general_channel_id,
            alpha_channel_id=config.ops_capture.alpha_channel_id,
            ticket_forum_id=config.ops_capture.ticket_forum_id,
            senior_staff_ids=set(config.ops_capture.senior_staff_ids),
        )
        ops_flat_channels = {
            c for c in (
                config.ops_capture.general_channel_id,
                config.ops_capture.alpha_channel_id,
            ) if c
        }
        if config.ops_capture.ticket_forum_id:
            ops_thread_parents = {config.ops_capture.ticket_forum_id}
        logger.info(
            "Ops capture enabled: flat=%s thread_parents=%s staff=%d",
            sorted(ops_flat_channels),
            sorted(ops_thread_parents),
            len(config.ops_capture.senior_staff_ids),
        )

    # --- Discord listener: push messages onto an async queue ---
    queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
    listener = DiscordListener(
        bot_token=config.discord.bot_token,
        monitored_channel_ids=config.discord.channel_ids(),
        queue=queue,
        activity_hook=_record_activity if activity_db is not None else None,
        activity_channel_ids=set(config.automations.activity_tracking_channel_ids),
        ops_hook=ops_capture.handle if ops_capture is not None else None,
        ops_flat_channel_ids=ops_flat_channels,
        ops_thread_parent_ids=ops_thread_parents,
    )

    # --- Track-record poster: public Discord channel of closed perps calls ---
    # Disabled gracefully when TRACK_RECORD_CHANNEL_ID is unset (channel_id=0).
    # Constructed after the listener so it can borrow the same Discord client.
    track_record_poster = None
    if config.track_record.enabled:
        from src.automations.track_record_poster import TrackRecordPoster

        track_record_poster = TrackRecordPoster(
            discord_client=listener.client,
            channel_id=config.track_record.channel_id,
            db_path=config.track_record.db_path,
            open_signals_db=open_signals_db,
            telegram_bot=telegram_bot,
            footer_url=config.track_record.footer_url,
        )
        await track_record_poster.open()
        logger.info(
            "Track-record poster enabled: channel_id=%d backfill_on_startup=%s",
            config.track_record.channel_id,
            config.track_record.backfill_on_startup,
        )
    else:
        logger.info(
            "Track-record poster disabled (set TRACK_RECORD_CHANNEL_ID to enable)",
        )

    # --- Router: classify + parse + format, enqueue to dispatcher ---
    router = Router(
        discord_cfg=config.discord,
        dispatcher=dispatcher,
        analytics=analytics,
        open_signals=open_signals_db,
        quick_trade_enabled=config.trading.enabled,
        image_archive=image_archive,
        executor_client=trade_executor_client,
        track_record_poster=track_record_poster,
    )

    # --- Email bot DB + sender (construction only; registration later) ---
    email_db: EmailDB | None = None
    email_events_db: EmailEventsDB | None = None
    email_sender: ResendClient | None = None
    email_worker: EmailWorker | None = None
    engagement_score_db: EngagementScoreDB | None = None
    if config.email_bot.enabled:
        if not config.email_bot.resend_api_key:
            logger.warning(
                "EMAIL_BOT_ENABLED=true but RESEND_API_KEY is empty; "
                "email bot disabled"
            )
        else:
            email_db = EmailDB(db_path=config.email_bot.db_path)
            await email_db.open()
            # Webhook event store (Resend stats + auto-suppression). Opens
            # alongside the main email DB so the webhook handler can write
            # the moment it's registered.
            email_events_db = EmailEventsDB(
                db_path=config.email_bot.email_events_db_path,
            )
            await email_events_db.open()
            # Engagement scoring shares the events DB file (same path) so
            # the recompute cron can scan email_events and write
            # engagement_score in one connection. The branch_evaluator
            # reads from this DB on every send to pick hot/warm/cold +
            # high/low variants. A separate handle so the events DB can
            # close cleanly during shutdown without orphaning the score
            # connection.
            engagement_score_db = EngagementScoreDB(
                db_path=config.email_bot.email_events_db_path,
            )
            await engagement_score_db.open()
            email_sender = ResendClient(
                api_key=config.email_bot.resend_api_key,
                from_address=config.email_bot.resend_from_address,
            )
            # ProtectedSender wraps the raw Resend client with the
            # last-mile suppression gate and a per-recipient throttle
            # (30 min) so worker.py never has to repeat the checks.
            protected_sender = ProtectedSender(
                client=email_sender,
                events_db=email_events_db,
                email_db=email_db,
            )
            # branch_evaluator context. Bundles the three data sources
            # the routing functions read from. Adapter classes wrap the
            # production whop_members_db and email_db handles so the
            # evaluator's Protocols stay structurally typed (anyone can
            # inject an in-memory stand-in for tests). The context is
            # only constructed when whop_members_db is available -- the
            # dunning and onboarding routes can't decide without it
            # anyway, but holding off on building the ctx keeps the
            # worker's branching path off entirely in deployments that
            # skip the Whop sync.
            branch_evaluator_ctx: EvalContext | None = None
            if whop_members_db is not None:
                branch_evaluator_ctx = EvalContext(
                    score_db=engagement_score_db,
                    members=_WhopMembersAdapter(whop_members_db),
                    subscribers=_SubscribersAdapter(email_db),
                )
            # Suppression-rate monitor. Records every delivery attempt
            # and every suppression at the last-mile gate, fires a
            # WARNING + optional Discord webhook alert when the rolling
            # one-hour rate breaches 5%. The webhook URL is opt-in via
            # OPS_ALERT_WEBHOOK_URL; unset means log-only alerting,
            # which still lands in the Railway log stream.
            suppression_metrics = SuppressionMetrics(
                webhook_url=os.getenv("OPS_ALERT_WEBHOOK_URL", ""),
            )
            email_worker = EmailWorker(
                db=email_db,
                sender=protected_sender,
                analytics_db_path="data/analytics.db",
                poll_interval_sec=config.email_bot.worker_poll_sec,
                max_per_cycle=config.email_bot.worker_max_per_cycle,
                # Bronze day-5 mints a single-use 30%-off Elite code at
                # send time. Reuses the same promo-scoped Whop key as the
                # save-offer router. Unset -> day 5 still sends, plain link.
                whop_promo_api_key=config.automations.whop_promo_api_key,
                whop_company_id=config.automations.whop_company_id,
                bronze_promo_ttl_days=config.automations.bronze_promo_ttl_days,
                # Last-mile suppression gate. With this wired, any send
                # whose recipient has previously bounced / complained /
                # unsubscribed gets canceled at delivery time -- even if
                # the row slipped through cancel_all_pending due to a
                # race with the bounce webhook.
                events_db=email_events_db,
                branch_evaluator_ctx=branch_evaluator_ctx,
                suppression_metrics=suppression_metrics,
            )
            # AUT-026 Targeted Save Offer Router. Reuses the cancel-survey
            # promo-creation key (same Whop scope: promo_code:create +
            # access_pass:basic:read). Disabled gracefully if the key is
            # unset — handler still fires, just falls back to bare rejoin
            # URLs without minting personalised promo codes.
            save_offer_router = SaveOfferRouter(
                email_db=email_db,
                promo_api_key=config.automations.whop_promo_api_key,
                whop_company_id=config.automations.whop_company_id,
                rejoin_url_base=config.email_bot.rejoin_url,
                promo_ttl_days=config.automations.cancel_survey_promo_ttl_days,
            )
            # Register aiohttp webhook routes on the shared OAuth callback app
            webhook_handlers = EmailWebhookHandlers(
                db=email_db,
                whop_webhook_secret=config.email_bot.whop_webhook_secret,
                admin_secret=config.email_bot.admin_webhook_secret,
                rejoin_url_default=config.email_bot.rejoin_url,
                whop_members_db=whop_members_db,
                resend_webhook_secret=config.email_bot.resend_webhook_secret,
                events_db=email_events_db,
                save_offer_router=save_offer_router,
                post_retention_survey_url=(
                    config.automations.post_retention_survey_url
                ),
                post_retention_delay_days=(
                    config.automations.post_retention_delay_days
                ),
                # Bronze -> Elite upsell enrolment. Dormant until the
                # Whop Bronze access-pass id is set; then a Bronze
                # membership.activated schedules the day 1/3/5 sequence.
                bronze_access_pass_id=(
                    config.automations.whop_bronze_access_pass_id
                ),
            )
            webhook_handlers.register(verification.callback_server.app)
            logger.info("Email bot enabled (Resend + Whop webhook + SaveOfferRouter)")

    # --- Automations crons (optional, controlled by AUTOMATIONS_ENABLED) ---
    launch_broadcaster: FeatureLaunchBroadcaster | None = None
    inactivity_detector: InactivityDetector | None = None
    inactivity_day10: InactivityDay10Email | None = None
    onboarding_sequence: OnboardingSequence | None = None
    dunning_sequence: DunningSequence | None = None
    pre_renewal_email: PreRenewalEmail | None = None
    pre_pause_return_email: PrePauseReturnEmail | None = None
    value_reminder: ValueReminder | None = None
    channel_feeler: ChannelFeeler | None = None
    whop_email_sync: WhopEmailSync | None = None
    whop_email_sync_cron: WhopEmailSyncCron | None = None
    whop_reviews_db: WhopReviewsDB | None = None
    whop_reviews_sync: WhopReviewsSync | None = None
    cancel_survey_dm: CancelSurveyDM | None = None
    if config.automations.enabled and activity_db is not None:
        launch_broadcaster = FeatureLaunchBroadcaster(
            telegram_bot=telegram_bot,
            verification_db=verification.db,
            resend_client=email_sender,
            cta_url=config.automations.launch_cta_url,
            whop_members_db=whop_members_db,
            # Last-mile suppression gate: broadcast skips any recipient
            # with a prior bounce / complaint / unsubscribe.
            events_db=email_events_db,
        )

        if email_db is not None:
            inactivity_detector = InactivityDetector(
                activity_db=activity_db,
                email_db=email_db,
                threshold_days=config.automations.inactivity_threshold_days,
                interval_hours=config.automations.inactivity_detector_interval_hours,
                rejoin_url=config.automations.launch_cta_url,
                whop_members_db=whop_members_db,
                verification_db=verification.db,
            )
            # 10-day inactivity (one-shot, distinct from 14-day reengagement).
            # Sits between the 5-day Concierge ping (Discord, separate)
            # and the 14-day reengagement series.
            if whop_members_db is not None:
                inactivity_day10 = InactivityDay10Email(
                    activity_db=activity_db,
                    whop_members_db=whop_members_db,
                    email_db=email_db,
                    threshold_days=10,
                    interval_hours=config.automations.inactivity_detector_interval_hours,
                    rejoin_url=config.automations.launch_cta_url,
                )
                # Onboarding 5-email sequence (Day 0/3/5/7/30).
                # SAFETY: go_live_at must be set (via ONBOARDING_GO_LIVE_AT_EPOCH
                # env var) so the cron can't retroactively onboard the
                # 121k+ historical whop_members roster. Default 0 means
                # the cron silently no-ops at startup; flip the env var
                # to NOW (epoch seconds) to begin onboarding for any
                # member synced AFTER that point.
                _onboarding_go_live = int(
                    os.environ.get("ONBOARDING_GO_LIVE_AT_EPOCH", "0") or "0"
                )
                onboarding_sequence = OnboardingSequence(
                    whop_members_db=whop_members_db,
                    email_db=email_db,
                    interval_hours=24,
                    rejoin_url=config.automations.launch_cta_url,
                    go_live_at=_onboarding_go_live,
                )
                # Failed-payment dunning (Day 0/3/10) — needs Whop
                # payment_failed webhook to set dunning_active=1.
                dunning_sequence = DunningSequence(
                    whop_members_db=whop_members_db,
                    email_db=email_db,
                    interval_hours=24,
                    rejoin_url=config.automations.launch_cta_url,
                )
                # Pre-renewal (3 days before billing). Dormant until
                # Whop sync starts populating current_period_end.
                pre_renewal_email = PreRenewalEmail(
                    whop_members_db=whop_members_db,
                    email_db=email_db,
                    days_before=3,
                    interval_hours=24,
                    rejoin_url=config.automations.launch_cta_url,
                )
                # Pre-pause-return (3 days before pause expires).
                # Dormant until Whop pause feature is wired.
                pre_pause_return_email = PrePauseReturnEmail(
                    whop_members_db=whop_members_db,
                    email_db=email_db,
                    days_before=3,
                    interval_hours=24,
                    rejoin_url=config.automations.launch_cta_url,
                )
            else:
                logger.info(
                    "Onboarding/dunning/pre-renewal/pre-pause-return/"
                    "10-day-inactivity skipped: whop_members_db not "
                    "available (set WHOP_API_KEY to enable)"
                )
        else:
            logger.info("Feature 2 (inactivity) skipped: email bot not enabled")

        value_reminder = ValueReminder(
            telegram_bot=telegram_bot,
            verification_db=verification.db,
            analytics_db=analytics,
            cycle_days=config.automations.value_reminder_cycle_days,
            interval_hours=config.automations.value_reminder_poll_interval_hours,
        )

        if email_sender is not None and config.automations.feeler_channel_variants:
            channel_feeler = ChannelFeeler(
                activity_db=activity_db,
                resend_client=email_sender,
                variant_by_channel=config.automations.feeler_channel_variants,
                low_engagement_threshold=config.automations.feeler_low_engagement_threshold,
                window_days=config.automations.feeler_window_days,
                cooldown_days=config.automations.feeler_cooldown_days,
                interval_hours=config.automations.feeler_detector_interval_hours,
                cta_url_by_variant={
                    "telegram_bot": config.automations.launch_cta_url,
                    "tools": config.automations.launch_cta_url,
                    "concierge": config.automations.launch_cta_url,
                },
                whop_members_db=whop_members_db,
                verification_db=verification.db,
            )
        else:
            logger.info(
                "Feature 4 (channel feeler) skipped: need Resend + "
                "feeler_channel_variants configured"
            )

        # Whop -> verified_users.email backfill. Runs once at startup (optional)
        # and on a 24h cron. Also exposed via the /sync-emails slash command.
        if config.automations.whop_api_key and config.automations.whop_company_id:
            # Bronze -> Elite upsell, piggybacked on the membership walk.
            # Needs the email DB; dormant unless the free product id +
            # go-live epoch are both configured (go-live default 0 ->
            # nobody enrolled, so the existing free backlog is never hit).
            bronze_upsell = None
            if email_db is not None:
                from src.automations.bronze_upsell import BronzeUpsell

                bronze_upsell = BronzeUpsell(
                    email_db=email_db,
                    free_product_id=config.automations.whop_bronze_free_product_id,
                    go_live_at=config.automations.bronze_enroll_go_live_at_epoch,
                    rejoin_url=config.email_bot.rejoin_url,
                )
                logger.info(
                    "BronzeUpsell wired (enabled=%s, free_product=%s, "
                    "go_live_at=%s)",
                    bronze_upsell.is_enabled,
                    config.automations.whop_bronze_free_product_id or "(unset)",
                    config.automations.bronze_enroll_go_live_at_epoch,
                )
            whop_email_sync = WhopEmailSync(
                verification_db=verification.db,
                api_key=config.automations.whop_api_key,
                company_id=config.automations.whop_company_id,
                api_base=config.automations.whop_api_base,
                members_db=whop_members_db,
                bronze_upsell=bronze_upsell,
            )
            whop_email_sync_cron = WhopEmailSyncCron(
                sync=whop_email_sync,
                interval_hours=config.automations.email_sync_interval_hours,
            )
            logger.info("Whop email sync enabled")
        else:
            logger.info(
                "Whop email sync skipped: need WHOP_API_KEY + WHOP_COMPANY_ID"
            )

        # Whop reviews -> Discord staff channel relay. Needs the API key + a
        # target channel ID. Skipped (with a log line) if either is missing so
        # the bot still boots for people who don't want the feature.
        if (
            config.automations.whop_api_key
            and config.automations.whop_company_id
            and config.automations.whop_reviews_channel_id
        ):
            whop_reviews_db = WhopReviewsDB(
                db_path=config.automations.whop_reviews_db_path,
            )
            await whop_reviews_db.open()
            whop_reviews_sync = WhopReviewsSync(
                db=whop_reviews_db,
                api_key=config.automations.whop_api_key,
                company_id=config.automations.whop_company_id,
                api_base=config.automations.whop_api_base,
                discord_client=listener.client,
                channel_id=config.automations.whop_reviews_channel_id,
                interval_seconds=config.automations.whop_reviews_interval_seconds,
                ping_on_low_stars=config.automations.whop_reviews_ping_on_low_stars,
            )
            logger.info("Whop reviews scanner enabled")
        else:
            logger.info(
                "Whop reviews scanner skipped: need WHOP_API_KEY + "
                "WHOP_COMPANY_ID + WHOP_REVIEWS_CHANNEL_ID",
            )

        # Cancel survey DM: watch for Elite role removals and DM the
        # cancelled member a personalised exit-survey link. Skipped (logged)
        # if either the Elite role ID or the survey URL is missing.
        survey_url = config.automations.cancel_survey_url
        elite_role_id_str = config.discord_oauth.elite_role_id
        if survey_url and elite_role_id_str:
            try:
                cancel_survey_dm = CancelSurveyDM(
                    client=listener.client,
                    elite_role_id=int(elite_role_id_str),
                    guild_id=config.discord.guild_id,
                    survey_url=survey_url,
                    db_path=config.automations.cancel_survey_db_path,
                    cooldown_seconds=config.automations.cancel_survey_cooldown_seconds,
                    # On Elite role removal, three things fire in parallel:
                    #   1. Discord DM the survey link (built via embed)
                    #   2. Enroll in 3-email winback sequence (whop_members
                    #      email lookup, verified_users fallback)
                    #   3. Email the survey link directly via Resend
                    # Together they replace the old WHOP_WEBHOOK_SECRET flow.
                    rejoin_url=config.email_bot.rejoin_url,
                    whop_members_db=whop_members_db,
                    verification_db=verification.db,
                    email_db=email_db,
                    resend_client=email_sender,
                    from_name="Potion Alpha Team",
                    # Per-user single-use promo codes: bot mints one code
                    # per discount offer (WELCOME20 / STAY30 / COMEBACK25)
                    # via Whop API before sending the DM. Codes travel in
                    # the URL so the survey form can display them without
                    # hardcoded values. Disabled if promo_api_key blank.
                    promo_api_key=config.automations.whop_promo_api_key,
                    whop_company_id=config.automations.whop_company_id,
                    promo_ttl_days=config.automations.cancel_survey_promo_ttl_days,
                )
                logger.info(
                    "Cancel survey DM watcher armed (with winback + email)",
                )
            except (ValueError, TypeError) as e:
                logger.warning("CancelSurveyDM init failed: %s", e)
        else:
            logger.info(
                "CancelSurveyDM skipped: need DISCORD_ELITE_ROLE_ID + "
                "CANCEL_SURVEY_URL"
            )

        logger.info("Automations enabled")

    # --- Email analytics (joins email_db + email_events_db + verified_users) ---
    email_analytics: EmailAnalytics | None = None
    if email_db is not None and email_events_db is not None:
        email_analytics = EmailAnalytics(
            email_db=email_db,
            events_db=email_events_db,
            verified_users_db_path=config.verification.db_path,
        )

    # --- Discord slash commands (build LAST so launch_broadcaster is available) ---
    if email_db is not None and config.email_bot.discord_admin_user_ids:
        slash = EmailSlashCommands(
            db=email_db,
            guild_id=config.discord.guild_id,
            admin_user_ids=set(config.email_bot.discord_admin_user_ids),
            default_rejoin_url=config.email_bot.rejoin_url,
            launch_broadcaster=launch_broadcaster,
            whop_email_sync=whop_email_sync,
            events_db=email_events_db,
            resend_client=email_sender,
            analytics=email_analytics,
            engagement_score_db=engagement_score_db,
        )
        slash.register(listener.client)
        logger.info("Discord slash commands registered")
    elif email_db is not None:
        logger.warning(
            "Email bot enabled but DISCORD_ADMIN_USER_IDS empty; "
            "slash commands will not be registered"
        )

    # --- Start everything in dependency order ---
    # Diagnostic: log which automation crons are wired up, before starting them.
    # If any of inactivity_day10, onboarding_sequence, dunning_sequence,
    # pre_renewal_email, pre_pause_return_email show as None here despite
    # whop_members_db + email_db both being non-None, the construction block
    # at lines 236-287 is being silently skipped.
    logger.info(
        "Cron readiness pre-start: "
        "email_worker=%s inactivity_detector=%s inactivity_day10=%s "
        "onboarding_sequence=%s dunning_sequence=%s pre_renewal_email=%s "
        "pre_pause_return_email=%s value_reminder=%s "
        "(email_db=%s whop_members_db=%s)",
        email_worker is not None, inactivity_detector is not None,
        inactivity_day10 is not None, onboarding_sequence is not None,
        dunning_sequence is not None, pre_renewal_email is not None,
        pre_pause_return_email is not None, value_reminder is not None,
        email_db is not None, whop_members_db is not None,
    )
    await verification.start()
    await dispatcher.start()
    if email_worker is not None:
        await email_worker.start()
    if inactivity_detector is not None:
        await inactivity_detector.start()
    if inactivity_day10 is not None:
        await inactivity_day10.start()
    if onboarding_sequence is not None:
        await onboarding_sequence.start()
    if dunning_sequence is not None:
        await dunning_sequence.start()
    if pre_renewal_email is not None:
        await pre_renewal_email.start()
    if pre_pause_return_email is not None:
        await pre_pause_return_email.start()
    if value_reminder is not None:
        await value_reminder.start()
    if channel_feeler is not None:
        await channel_feeler.start()
    if whop_email_sync is not None and config.automations.email_sync_on_startup:
        # Kick off a startup sync but don't block startup on it. Failures are
        # logged inside run_once().
        asyncio.create_task(whop_email_sync.run_once(), name="whop_email_sync_startup")
    if whop_email_sync_cron is not None:
        await whop_email_sync_cron.start()
    if whop_reviews_sync is not None:
        await whop_reviews_sync.start()
    if cancel_survey_dm is not None:
        await cancel_survey_dm.open()
    listener_task = asyncio.create_task(listener.start(), name="discord_listener")
    consumer_task = asyncio.create_task(
        _consume_queue(queue, router, shutdown), name="queue_consumer",
    )

    # --- Track-record backfill: one-off walk of recent closes at startup ---
    # Waits for the Discord client to be ready (so fetch_channel works) and
    # then posts anything not already in the idempotency table. Runs as a
    # background task so we don't block the shutdown signal handler.
    if (
        track_record_poster is not None
        and config.track_record.backfill_on_startup
    ):
        async def _track_record_backfill_startup() -> None:
            try:
                await listener.client.wait_until_ready()
                # Every forwarded channel contributes to the track-record;
                # mirror channels never have lifecycle events recorded in
                # open_signals so they're harmlessly absent here too.
                eligible = config.discord.channel_ids()
                names = {
                    ch.channel_id: f"#{ch.name}"
                    for ch in config.discord.channels
                }
                count = await track_record_poster.backfill(
                    days=config.track_record.backfill_days,
                    eligible_channel_ids=eligible,
                    channel_display_names=names,
                    pace_sec=config.track_record.backfill_pace_sec,
                )
                logger.info(
                    "Track-record startup backfill posted %d call(s)", count,
                )
            except Exception:
                logger.exception("Track-record startup backfill crashed")

        asyncio.create_task(
            _track_record_backfill_startup(),
            name="track_record_backfill_startup",
        )

    active_user_count = await verification.db.count_active()
    logger.info(
        "Bot started — monitoring %d channel(s), %d verified user(s), "
        "dispatcher rate=%.1f/s",
        len(config.discord.channels),
        active_user_count,
        config.dispatcher.rate_per_sec,
    )

    try:
        await shutdown.wait()
    finally:
        logger.info("Shutdown requested")
        consumer_task.cancel()
        try:
            await listener.stop()
        except Exception:
            logger.exception("Listener stop error")
        listener_task.cancel()
        for task in (listener_task, consumer_task):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await dispatcher.stop()
        except Exception:
            logger.exception("Dispatcher stop error")
        for cron, name in (
            (whop_reviews_sync, "whop_reviews_sync"),
            (whop_email_sync_cron, "whop_email_sync_cron"),
            (channel_feeler, "channel_feeler"),
            (value_reminder, "value_reminder"),
            (pre_pause_return_email, "pre_pause_return_email"),
            (pre_renewal_email, "pre_renewal_email"),
            (dunning_sequence, "dunning_sequence"),
            (onboarding_sequence, "onboarding_sequence"),
            (inactivity_day10, "inactivity_day10"),
            (inactivity_detector, "inactivity_detector"),
        ):
            if cron is not None:
                try:
                    await cron.stop()
                except Exception:
                    logger.exception("%s stop error", name)
        if activity_db is not None:
            try:
                await activity_db.close()
            except Exception:
                logger.exception("Activity DB close error")
        if ops_db is not None:
            try:
                await ops_db.close()
            except Exception:
                logger.exception("Ops DB close error")
        if whop_members_db is not None:
            try:
                await whop_members_db.close()
            except Exception:
                logger.exception("Whop members DB close error")
        if whop_reviews_db is not None:
            try:
                await whop_reviews_db.close()
            except Exception:
                logger.exception("Whop reviews DB close error")
        if cancel_survey_dm is not None:
            try:
                await cancel_survey_dm.close()
            except Exception:
                logger.exception("Cancel survey DM close error")
        if email_worker is not None:
            try:
                await email_worker.stop()
            except Exception:
                logger.exception("Email worker stop error")
        if email_sender is not None:
            try:
                await email_sender.close()
            except Exception:
                logger.exception("Email sender close error")
        if email_db is not None:
            try:
                await email_db.close()
            except Exception:
                logger.exception("Email DB close error")
        if email_events_db is not None:
            try:
                await email_events_db.close()
            except Exception:
                logger.exception("Email events DB close error")
        if engagement_score_db is not None:
            try:
                await engagement_score_db.close()
            except Exception:
                logger.exception("Engagement score DB close error")
        if image_archive is not None:
            try:
                await image_archive.close()
            except Exception:
                logger.exception("Image archive close error")
        if track_record_poster is not None:
            try:
                await track_record_poster.close()
            except Exception:
                logger.exception("Track-record poster close error")
        if trade_executor_client is not None:
            try:
                await trade_executor_client.close()
            except Exception:
                logger.exception("Trade executor client close error")
        if delegates_db is not None:
            try:
                await delegates_db.close()
            except Exception:
                logger.exception("Trading delegates DB close error")
        if trading_settings_db is not None:
            try:
                await trading_settings_db.close()
            except Exception:
                logger.exception("Trading settings DB close error")
        try:
            await verification.stop()
        except Exception:
            logger.exception("Verification stop error")
        try:
            await analytics.close()
        except Exception:
            logger.exception("Analytics close error")
        try:
            await open_signals_db.close()
        except Exception:
            logger.exception("OpenSignalsDB close error")
        logger.info("Shutdown complete")


def main() -> None:
    config = load_config()
    setup_logging(config.logging)
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
