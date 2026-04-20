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
import logging
import signal

from telegram import Bot

from src.analytics import AnalyticsDB
from src.automations import ActivityDB
from src.automations.cancel_survey_dm import CancelSurveyDM
from src.automations.channel_feeler import ChannelFeeler
from src.automations.feature_launch import FeatureLaunchBroadcaster
from src.automations.inactivity_detector import InactivityDetector
from src.automations.value_reminder import ValueReminder
from src.automations.whop_email_sync import WhopEmailSync, WhopEmailSyncCron
from src.automations.whop_members_db import WhopMembersDB
from src.automations.whop_reviews_sync import WhopReviewsDB, WhopReviewsSync
from src.config import Config, load_config
from src.discord_listener import DiscordListener, IncomingMessage
from src.dispatcher import Dispatcher
from src.email_bot import EmailDB
from src.email_bot.discord_commands import EmailSlashCommands
from src.email_bot.sender import ResendClient
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

    # --- Router: classify + parse + format, enqueue to dispatcher ---
    router = Router(
        discord_cfg=config.discord,
        dispatcher=dispatcher,
        analytics=analytics,
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

    # --- Discord listener: push messages onto an async queue ---
    queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
    listener = DiscordListener(
        bot_token=config.discord.bot_token,
        monitored_channel_ids=config.discord.channel_ids(),
        queue=queue,
        activity_hook=_record_activity if activity_db is not None else None,
        activity_channel_ids=set(config.automations.activity_tracking_channel_ids),
    )

    # --- Email bot DB + sender (construction only; registration later) ---
    email_db: EmailDB | None = None
    email_sender: ResendClient | None = None
    email_worker: EmailWorker | None = None
    if config.email_bot.enabled:
        if not config.email_bot.resend_api_key:
            logger.warning(
                "EMAIL_BOT_ENABLED=true but RESEND_API_KEY is empty; "
                "email bot disabled"
            )
        else:
            email_db = EmailDB(db_path=config.email_bot.db_path)
            await email_db.open()
            email_sender = ResendClient(
                api_key=config.email_bot.resend_api_key,
                from_address=config.email_bot.resend_from_address,
            )
            email_worker = EmailWorker(
                db=email_db,
                sender=email_sender,
                analytics_db_path="data/analytics.db",
                poll_interval_sec=config.email_bot.worker_poll_sec,
                max_per_cycle=config.email_bot.worker_max_per_cycle,
            )
            # Register aiohttp webhook routes on the shared OAuth callback app
            webhook_handlers = EmailWebhookHandlers(
                db=email_db,
                whop_webhook_secret=config.email_bot.whop_webhook_secret,
                admin_secret=config.email_bot.admin_webhook_secret,
                rejoin_url_default=config.email_bot.rejoin_url,
            )
            webhook_handlers.register(verification.callback_server.app)
            logger.info("Email bot enabled (Resend + Whop webhook)")

    # --- Automations crons (optional, controlled by AUTOMATIONS_ENABLED) ---
    launch_broadcaster: FeatureLaunchBroadcaster | None = None
    inactivity_detector: InactivityDetector | None = None
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
            whop_email_sync = WhopEmailSync(
                verification_db=verification.db,
                api_key=config.automations.whop_api_key,
                company_id=config.automations.whop_company_id,
                api_base=config.automations.whop_api_base,
                members_db=whop_members_db,
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

    # --- Discord slash commands (build LAST so launch_broadcaster is available) ---
    if email_db is not None and config.email_bot.discord_admin_user_ids:
        slash = EmailSlashCommands(
            db=email_db,
            guild_id=config.discord.guild_id,
            admin_user_ids=set(config.email_bot.discord_admin_user_ids),
            default_rejoin_url=config.email_bot.rejoin_url,
            launch_broadcaster=launch_broadcaster,
            whop_email_sync=whop_email_sync,
        )
        slash.register(listener.client)
        logger.info("Discord slash commands registered")
    elif email_db is not None:
        logger.warning(
            "Email bot enabled but DISCORD_ADMIN_USER_IDS empty; "
            "slash commands will not be registered"
        )

    # --- Start everything in dependency order ---
    await verification.start()
    await dispatcher.start()
    if email_worker is not None:
        await email_worker.start()
    if inactivity_detector is not None:
        await inactivity_detector.start()
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
        try:
            await verification.stop()
        except Exception:
            logger.exception("Verification stop error")
        try:
            await analytics.close()
        except Exception:
            logger.exception("Analytics close error")
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
