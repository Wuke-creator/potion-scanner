"""Discord slash commands for the email bot.

Registered on the SAME discord.Client that hosts the signal listener,
so everything lives under the one Potion Scanner bot identity.

Commands (admin-only, checked against DISCORD_ADMIN_IDS env var):

  /email-status
    Show counts of pending / sent / failed scheduled emails.

  /email-test <email> <sequence> <day> [reason]
    Queue a single email for immediate delivery. Use to preview
    templates before going live.

  /email-enroll <email> <trigger> [reason] [name]
    Manually enroll someone in the 4-email sequence (shortcut for
    staff-triggered win-back when Whop webhook hasn't fired).

The commands are registered as guild-scoped (not global) because guild
commands update instantly, global commands take ~1 hour to propagate.
"""

from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord import app_commands

from src.automations.feature_launch import FeatureLaunchBroadcaster
from src.automations.whop_email_sync import WhopEmailSync
from src.email_bot.analytics import EmailAnalytics
from src.email_bot.db import EmailDB, Subscriber
from src.email_bot.engagement_scoring import EngagementScoreDB
from src.email_bot.events_db import EmailEventsDB
from src.email_bot.sender import ResendClient
from src.email_bot.webhook import normalize_reason

logger = logging.getLogger(__name__)


def _format_window(seconds: int) -> str:
    """Render a seconds count as 'N day(s)' / 'N hour(s)' for stats output."""
    if seconds <= 0:
        return "0 days"
    days = seconds // 86400
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''}"
    hours = seconds // 3600
    return f"{hours} hour{'s' if hours != 1 else ''}"


def _render_sequence_stats(
    sequence: str, day: int | None, stats: dict,
) -> str:
    """Format the /email-sequence-stats response. Mirrors
    _render_broadcast_stats so operators see the same shape regardless
    of whether they're looking at a Resend broadcast or a transactional
    sequence."""
    sent = int(stats.get("sent", 0))
    sent_events = int(stats.get("sent_events", sent))
    delivered = int(stats.get("delivered", 0))
    opened = int(stats.get("opened", 0))
    clicked = int(stats.get("clicked", 0))
    bounced = int(stats.get("bounced", 0))
    complained = int(stats.get("complained", 0))
    failed = int(stats.get("failed", 0))
    hard_bounced = int(stats.get("hard_bounced", 0))
    soft_bounced = int(stats.get("soft_bounced", 0))
    unique_openers = int(stats.get("unique_openers", 0))
    unique_clickers = int(stats.get("unique_clickers", 0))

    delivery_rate = float(stats.get("delivery_rate", 0.0))
    open_rate = float(stats.get("open_rate", 0.0))
    click_rate_delivered = float(stats.get("click_rate_delivered", 0.0))
    click_rate_opened = float(stats.get("click_rate_opened", 0.0))
    bounce_rate = float(stats.get("bounce_rate", 0.0))
    complaint_rate = float(stats.get("complaint_rate", 0.0))
    fail_rate = float(stats.get("fail_rate", 0.0))

    window = _format_window(int(stats.get("window_seconds", 0)))
    label = sequence
    if day is not None:
        label = f"{sequence} day {day}"

    lines = [
        f'Sequence stats: "{label}" (last {window})',
        f"  Sent:        {sent:>9,}",
    ]
    if sent_events != sent:
        lines.append(
            f"    (Resend webhook sent events: {sent_events:,} — "
            f"webhook lag in progress)",
        )
    lines.extend([
        f"  Delivered:   {delivered:>9,}    ({delivery_rate * 100:.1f}%)",
        f"  Opened:      {opened:>9,}    "
        f"({open_rate * 100:.1f}% of delivered, "
        f"{unique_openers:,} unique)",
        f"  Clicked:     {clicked:>9,}    "
        f"({click_rate_delivered * 100:.1f}% of delivered, "
        f"{click_rate_opened * 100:.1f}% of opens, "
        f"{unique_clickers:,} unique)",
        f"  Bounced:     {bounced:>9,}    "
        f"({bounce_rate * 100:.1f}%, "
        f"{hard_bounced:,} hard / {soft_bounced:,} soft)",
        f"  Complaints:  {complained:>9,}    ({complaint_rate * 100:.3f}%)",
        f"  Failed:      {failed:>9,}    ({fail_rate * 100:.2f}%)",
    ])

    top_clicks = stats.get("top_clicked_urls") or []
    if top_clicks:
        lines.append("")
        lines.append("  Top CTAs:")
        for i, item in enumerate(top_clicks, start=1):
            url = str(item.get("url") or "")
            count = int(item.get("count", 0))
            lines.append(f"    {i}. {url}   ({count:,} clicks)")

    return "\n".join(lines)


def _render_day0_funnel(stats: dict) -> str:
    """Format the /email-day0-funnel response. Renders a vertical funnel
    of sent → delivered → opened → clicked → telegram_verified with
    stepwise rates."""
    sent = int(stats.get("sent", 0))
    delivered = int(stats.get("delivered", 0))
    opened = int(stats.get("opened", 0))
    clicked = int(stats.get("clicked", 0))
    verified = int(stats.get("telegram_verified", 0))

    delivered_rate = float(stats.get("delivered_rate", 0.0)) * 100
    open_rate = float(stats.get("open_rate", 0.0)) * 100
    click_rate = float(stats.get("click_rate", 0.0)) * 100
    verify_rate = float(stats.get("verify_rate", 0.0)) * 100

    window = _format_window(int(stats.get("window_seconds", 0)))
    conv = _format_window(int(stats.get("conversion_window_seconds", 0)))

    lines = [
        f"Day 0 onboarding funnel (last {window}, "
        f"verification window: {conv})",
        f"  Sent:                  {sent:>9,}",
        f"  Delivered:             {delivered:>9,}    "
        f"({delivered_rate:.1f}% of sent)",
        f"  Opened:                {opened:>9,}    "
        f"({open_rate:.1f}% of delivered)",
        f"  Clicked:               {clicked:>9,}    "
        f"({click_rate:.1f}% of opens)",
        f"  Telegram-verified:     {verified:>9,}    "
        f"({verify_rate:.1f}% of sent — top-of-funnel conversion)",
    ]
    return "\n".join(lines)


def _render_broadcast_stats(
    broadcast_id: str, title: str, stats: dict,
) -> str:
    """Format the /email-broadcast-stats response.

    Pulled out of the slash command so tests can assert on the rendered
    string without needing a Discord interaction. Numbers come in as
    plain ints/floats from EmailEventsDB.broadcast_stats.

    No em dashes anywhere (per Luke's writing rules); the title sits
    after a colon.
    """
    sent = int(stats.get("sent", 0))
    delivered = int(stats.get("delivered", 0))
    opened = int(stats.get("opened", 0))
    clicked = int(stats.get("clicked", 0))
    bounced = int(stats.get("bounced", 0))
    complained = int(stats.get("complained", 0))
    failed = int(stats.get("failed", 0))
    hard_bounced = int(stats.get("hard_bounced", 0))
    soft_bounced = int(stats.get("soft_bounced", 0))

    delivery_rate = float(stats.get("delivery_rate", 0.0))
    open_rate = float(stats.get("open_rate", 0.0))
    click_rate_delivered = float(stats.get("click_rate_delivered", 0.0))
    click_rate_opened = float(stats.get("click_rate_opened", 0.0))
    bounce_rate = float(stats.get("bounce_rate", 0.0))
    complaint_rate = float(stats.get("complaint_rate", 0.0))
    fail_rate = float(stats.get("fail_rate", 0.0))

    safe_title = title.strip() or "(title unavailable)"
    lines = [
        f'Broadcast {broadcast_id}: "{safe_title}"',
        f"  Sent:        {sent:>9,}",
        f"  Delivered:   {delivered:>9,}    ({delivery_rate * 100:.1f}%)",
        f"  Opened:      {opened:>9,}    "
        f"({open_rate * 100:.1f}% of delivered)",
        f"  Clicked:     {clicked:>9,}    "
        f"({click_rate_delivered * 100:.1f}% of delivered, "
        f"{click_rate_opened * 100:.1f}% of opens)",
        f"  Bounced:     {bounced:>9,}    "
        f"({bounce_rate * 100:.1f}%, "
        f"{hard_bounced:,} hard / {soft_bounced:,} soft)",
        f"  Complaints:  {complained:>9,}    ({complaint_rate * 100:.3f}%)",
        f"  Failed:      {failed:>9,}    ({fail_rate * 100:.2f}%)",
    ]

    top_clicks = stats.get("top_clicked_urls") or []
    if top_clicks:
        lines.append("")
        lines.append("  Top CTAs:")
        for i, item in enumerate(top_clicks, start=1):
            url = str(item.get("url") or "")
            count = int(item.get("count", 0))
            lines.append(f"    {i}. {url}   ({count:,} clicks)")

    return "\n".join(lines)


def _render_engagement_snapshot(
    counts: dict, top: list[dict], transitions: list[dict],
) -> str:
    """Format the /engagement-snapshot response.

    Pulled out of the command so tests can assert on the rendered
    string without spinning up a Discord interaction. Counts come
    in as {hot, warm, cold}; top / transitions are lists of
    {email, score, tier, recorded_at/updated_at} dicts.
    """
    hot = int(counts.get("hot", 0))
    warm = int(counts.get("warm", 0))
    cold = int(counts.get("cold", 0))
    total = hot + warm + cold

    def _pct(n: int) -> str:
        return f"{(n / total * 100):.1f}%" if total else "n/a"

    lines = [
        "**Engagement snapshot**",
        f"Total scored recipients: {total:,}",
        f"  Hot:  {hot:>6,}    ({_pct(hot)})",
        f"  Warm: {warm:>6,}    ({_pct(warm)})",
        f"  Cold: {cold:>6,}    ({_pct(cold)})",
    ]

    if top:
        lines.append("")
        lines.append("**Top 5 most-engaged recipients:**")
        for i, row in enumerate(top[:5], start=1):
            lines.append(
                f"  {i}. {row['email']}  "
                f"(score {row['score']:.1f}, {row['tier']})"
            )
    else:
        lines.append("")
        lines.append("**Top 5 most-engaged recipients:** (none)")

    if transitions:
        lines.append("")
        lines.append("**Last 5 tier transitions:**")
        for row in transitions[:5]:
            ts = int(row.get("recorded_at", 0))
            when = (
                time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))
                if ts else "n/a"
            )
            lines.append(
                f"  - {row['email']}  -> {row['tier']}  "
                f"(score {row['score']:.1f}, {when} UTC)"
            )
    else:
        lines.append("")
        lines.append("**Last 5 tier transitions:** (none)")

    return "\n".join(lines)


class EmailSlashCommands:
    """Admin-only email + automations operations as Discord slash commands."""

    def __init__(
        self,
        db: EmailDB,
        guild_id: int,
        admin_user_ids: set[int],
        default_rejoin_url: str,
        launch_broadcaster: FeatureLaunchBroadcaster | None = None,
        whop_email_sync: WhopEmailSync | None = None,
        events_db: EmailEventsDB | None = None,
        resend_client: ResendClient | None = None,
        analytics: EmailAnalytics | None = None,
        engagement_score_db: EngagementScoreDB | None = None,
    ):
        self._db = db
        self._guild_id = guild_id
        self._admin_ids = admin_user_ids
        self._default_rejoin = default_rejoin_url
        self._launch_broadcaster = launch_broadcaster
        self._whop_email_sync = whop_email_sync
        self._events_db = events_db
        self._resend_client = resend_client
        self._analytics = analytics
        self._engagement_score_db = engagement_score_db

    def register(self, client: discord.Client) -> None:
        """Attach a CommandTree to the discord.Client and wire our commands."""
        tree = app_commands.CommandTree(client)
        guild = discord.Object(id=self._guild_id)

        @tree.command(
            name="email-status",
            description="Email bot: count of pending / sent / failed sends.",
            guild=guild,
        )
        async def email_status(interaction: discord.Interaction) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message(
                    "Admin only.", ephemeral=True,
                )
                return
            counts = await self._db.count_by_status()
            lines = ["**Email pipeline:**"]
            for status in ("pending", "sent", "failed", "canceled"):
                lines.append(f"  {status}: {counts.get(status, 0)}")
            await interaction.response.send_message(
                "\n".join(lines), ephemeral=True,
            )

        @tree.command(
            name="email-test",
            description=(
                "Email bot: queue one template for immediate delivery (preview)."
            ),
            guild=guild,
        )
        @app_commands.describe(
            email="Recipient email address",
            sequence="winback or reengagement",
            day="1, 3, 5, or 7",
            reason="Exit reason (winback day 5 only, defaults to 'other')",
            name="Recipient first name (optional)",
        )
        async def email_test(
            interaction: discord.Interaction,
            email: str,
            sequence: str,
            day: int,
            reason: str | None = None,
            name: str | None = None,
        ) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message(
                    "Admin only.", ephemeral=True,
                )
                return
            if sequence not in ("winback", "reengagement"):
                await interaction.response.send_message(
                    "sequence must be 'winback' or 'reengagement'",
                    ephemeral=True,
                )
                return
            if day not in (1, 3, 5, 7):
                await interaction.response.send_message(
                    "day must be 1, 3, 5, or 7", ephemeral=True,
                )
                return
            norm_reason = normalize_reason(reason)
            sub = Subscriber(
                email=email.lower().strip(),
                name=(name or "").strip(),
                trigger_type="admin_test",
                exit_reason=norm_reason,
                rejoin_url=self._default_rejoin,
                created_at=int(time.time()),
            )
            await self._db.upsert_subscriber(sub)
            send_id = await self._db.schedule_one(
                email=sub.email, sequence=sequence, day=day,
                due_at=int(time.time()),
            )
            await interaction.response.send_message(
                f"Queued test: {sequence} day {day} to {email} "
                f"(reason={norm_reason}, send_id={send_id}). "
                "Worker will deliver on next cycle.",
                ephemeral=True,
            )

        @tree.command(
            name="email-enroll",
            description=(
                "Email bot: manually enroll someone in a 4-email sequence."
            ),
            guild=guild,
        )
        @app_commands.describe(
            email="Recipient email address",
            trigger="cancellation or inactivity",
            reason="Exit reason (cancellation only)",
            name="Recipient first name (optional)",
        )
        async def email_enroll(
            interaction: discord.Interaction,
            email: str,
            trigger: str,
            reason: str | None = None,
            name: str | None = None,
        ) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message(
                    "Admin only.", ephemeral=True,
                )
                return
            if trigger not in ("cancellation", "inactivity"):
                await interaction.response.send_message(
                    "trigger must be 'cancellation' or 'inactivity'",
                    ephemeral=True,
                )
                return
            norm_reason = normalize_reason(reason) if trigger == "cancellation" else "none"
            sub = Subscriber(
                email=email.lower().strip(),
                name=(name or "").strip(),
                trigger_type=trigger,
                exit_reason=norm_reason,
                rejoin_url=self._default_rejoin,
                created_at=int(time.time()),
            )
            await self._db.upsert_subscriber(sub)
            sequence = "winback" if trigger == "cancellation" else "reengagement"
            ids = await self._db.schedule_sequence(
                email=sub.email, sequence=sequence,
            )
            await interaction.response.send_message(
                f"Enrolled {email} in {sequence} sequence. "
                f"Scheduled 4 sends (ids={ids}). Day 1 delivers in ~24h.",
                ephemeral=True,
            )

        @tree.command(
            name="broadcast-feature",
            description=(
                "Broadcast a 'new feature shipped' DM (+ optional email) to all verified users."
            ),
            guild=guild,
        )
        @app_commands.describe(
            title="Short feature title, e.g. 'Perp Bot v2'",
            description="1-2 sentences explaining what it does and why it matters",
            include_email="Also send the email half? Default: yes",
            audience="Email audience: 'active' (default), 'churned', or 'all'",
        )
        async def broadcast_feature(
            interaction: discord.Interaction,
            title: str,
            description: str,
            include_email: bool = True,
            audience: str = "active",
        ) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message(
                    "Admin only.", ephemeral=True,
                )
                return
            if self._launch_broadcaster is None:
                await interaction.response.send_message(
                    "Launch broadcaster not wired. Enable automations in config.",
                    ephemeral=True,
                )
                return
            if not (1 <= len(title) <= 80):
                await interaction.response.send_message(
                    "title must be 1-80 chars.", ephemeral=True,
                )
                return
            if not (1 <= len(description) <= 500):
                await interaction.response.send_message(
                    "description must be 1-500 chars.", ephemeral=True,
                )
                return
            if audience not in ("active", "churned", "all"):
                await interaction.response.send_message(
                    "audience must be 'active', 'churned', or 'all'.",
                    ephemeral=True,
                )
                return

            # Defer because broadcasting can take 30s+ for large audiences
            await interaction.response.defer(ephemeral=True, thinking=True)
            stats = await self._launch_broadcaster.broadcast(
                title=title,
                description=description,
                include_email=include_email,
                audience=audience,
            )
            await interaction.followup.send(
                f"Feature launch complete (audience={audience}):\n"
                f"  DM: {stats.dm_sent}/{stats.dm_attempted} sent "
                f"(blocked {stats.dm_blocked}, failed {stats.dm_failed})\n"
                f"  Email: {stats.email_sent}/{stats.email_attempted} sent "
                f"(failed {stats.email_failed})\n"
                f"  Duration: {stats.duration_sec:.1f}s",
                ephemeral=True,
            )

        @tree.command(
            name="broadcast-monthly",
            description=(
                "Queue the monthly digest email for the chosen audience "
                "(active, churned, or all)."
            ),
            guild=guild,
        )
        @app_commands.describe(
            audience="Audience: 'active' (default Elite roster), 'churned', or 'all'",
        )
        async def broadcast_monthly(
            interaction: discord.Interaction,
            audience: str = "active",
        ) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message(
                    "Admin only.", ephemeral=True,
                )
                return
            if self._launch_broadcaster is None:
                await interaction.response.send_message(
                    "Launch broadcaster not wired. Enable automations in config.",
                    ephemeral=True,
                )
                return
            if audience not in ("active", "churned", "all"):
                await interaction.response.send_message(
                    "audience must be 'active', 'churned', or 'all'.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                result = await self._launch_broadcaster.enqueue_monthly_digest(
                    email_db=self._db,
                    audience=audience,
                    rejoin_url=self._default_rejoin,
                )
            except RuntimeError as e:
                await interaction.followup.send(
                    f"Cannot enqueue digest: {e}", ephemeral=True,
                )
                return
            await interaction.followup.send(
                f"Monthly digest queued (audience={result['audience']}):\n"
                f"  Queued:           {result['queued']:,}\n"
                f"  Skipped no email: {result['skipped_no_email']:,}\n\n"
                f"The EmailWorker (1.5/sec throttle) will drain the queue. "
                f"Track delivery via /email-broadcast-stats once a Resend "
                f"broadcast id appears, or watch /email-status for sent/failed counts.",
                ephemeral=True,
            )

        @tree.command(
            name="sync-emails",
            description=(
                "Email bot: sync emails from Whop API into verified_users."
            ),
            guild=guild,
        )
        async def sync_emails(interaction: discord.Interaction) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message(
                    "Admin only.", ephemeral=True,
                )
                return
            if self._whop_email_sync is None:
                await interaction.response.send_message(
                    "Whop email sync not configured. Set WHOP_API_KEY + "
                    "WHOP_COMPANY_ID in .env and restart.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            summary = await self._whop_email_sync.run_once()
            lines = ["**Whop email sync complete:**"]
            for key in ("status", "active_users", "needs_email", "matched",
                        "updated", "unmatched", "duration_sec"):
                if key in summary:
                    lines.append(f"  {key}: {summary[key]}")
            await interaction.followup.send(
                "\n".join(lines), ephemeral=True,
            )

        @tree.command(
            name="email-broadcast-stats",
            description=(
                "Email bot: per-broadcast send/open/click/bounce stats."
            ),
            guild=guild,
        )
        @app_commands.describe(
            broadcast_id="Resend broadcast UUID (find it in the Resend dashboard URL)",
        )
        async def email_broadcast_stats(
            interaction: discord.Interaction, broadcast_id: str,
        ) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message(
                    "Admin only.", ephemeral=True,
                )
                return
            if self._events_db is None:
                await interaction.response.send_message(
                    "Events DB not wired. Set RESEND_WEBHOOK_SECRET and "
                    "restart the bot.",
                    ephemeral=True,
                )
                return
            broadcast_id = broadcast_id.strip()
            if not broadcast_id:
                await interaction.response.send_message(
                    "broadcast_id is required.", ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            stats = await self._events_db.broadcast_stats(broadcast_id)

            title = "(title unavailable)"
            if self._resend_client is not None:
                try:
                    meta = await self._resend_client.get_broadcast(broadcast_id)
                except Exception:
                    logger.exception(
                        "get_broadcast failed for %s", broadcast_id,
                    )
                    meta = None
                if meta and isinstance(meta.get("name"), str) and meta["name"]:
                    title = meta["name"]

            body = _render_broadcast_stats(broadcast_id, title, stats)
            await interaction.followup.send(body, ephemeral=True)

        @tree.command(
            name="email-sequence-stats",
            description=(
                "Email bot: open/click/bounce stats for a sequence "
                "(onboarding, winback, dunning, etc)."
            ),
            guild=guild,
        )
        @app_commands.describe(
            sequence=(
                "onboarding | winback | reengagement | dunning | "
                "pre_renewal | pre_pause_return | inactive_day10"
            ),
            day="Specific day offset (omit to aggregate all days)",
            days_back="Trailing window in days (default 30)",
        )
        async def email_sequence_stats(
            interaction: discord.Interaction,
            sequence: str,
            day: int | None = None,
            days_back: int = 30,
        ) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message(
                    "Admin only.", ephemeral=True,
                )
                return
            if self._analytics is None:
                await interaction.response.send_message(
                    "Analytics not wired. Set RESEND_WEBHOOK_SECRET and "
                    "restart the bot.",
                    ephemeral=True,
                )
                return
            sequence = (sequence or "").strip()
            if sequence not in EmailDB.KNOWN_SEQUENCES:
                await interaction.response.send_message(
                    f"Unknown sequence '{sequence}'. Valid: "
                    f"{', '.join(sorted(EmailDB.KNOWN_SEQUENCES))}.",
                    ephemeral=True,
                )
                return
            if days_back < 1 or days_back > 365:
                await interaction.response.send_message(
                    "days_back must be between 1 and 365.", ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                stats = await self._analytics.sequence_stats(
                    sequence=sequence, day=day, days_back=days_back,
                )
            except Exception:
                logger.exception(
                    "sequence_stats crashed for %s day=%s", sequence, day,
                )
                await interaction.followup.send(
                    "Stats query crashed. Check logs.", ephemeral=True,
                )
                return
            body = _render_sequence_stats(sequence, day, stats)
            await interaction.followup.send(body, ephemeral=True)

        @tree.command(
            name="email-day0-funnel",
            description=(
                "Email bot: Day 0 onboarding -> Telegram-verified "
                "conversion funnel."
            ),
            guild=guild,
        )
        @app_commands.describe(
            days_back="Trailing window in days (default 30)",
            conversion_window_days=(
                "Window after Day 0 send to count a verification (default 7)"
            ),
        )
        async def email_day0_funnel(
            interaction: discord.Interaction,
            days_back: int = 30,
            conversion_window_days: int = 7,
        ) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message(
                    "Admin only.", ephemeral=True,
                )
                return
            if self._analytics is None:
                await interaction.response.send_message(
                    "Analytics not wired. Set RESEND_WEBHOOK_SECRET and "
                    "restart the bot.",
                    ephemeral=True,
                )
                return
            if days_back < 1 or days_back > 365:
                await interaction.response.send_message(
                    "days_back must be between 1 and 365.", ephemeral=True,
                )
                return
            if conversion_window_days < 1 or conversion_window_days > 90:
                await interaction.response.send_message(
                    "conversion_window_days must be between 1 and 90.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                stats = await self._analytics.onboarding_day0_funnel(
                    days_back=days_back,
                    conversion_window_days=conversion_window_days,
                )
            except Exception:
                logger.exception("onboarding_day0_funnel crashed")
                await interaction.followup.send(
                    "Funnel query crashed. Check logs.", ephemeral=True,
                )
                return
            body = _render_day0_funnel(stats)
            await interaction.followup.send(body, ephemeral=True)

        @tree.command(
            name="engagement-snapshot",
            description=(
                "Email bot: hot/warm/cold tier distribution + top 5 + "
                "recent transitions."
            ),
            guild=guild,
        )
        async def engagement_snapshot(
            interaction: discord.Interaction,
        ) -> None:
            # Admin-only, matching the gating used by every other
            # /email-* command. The engagement table can contain PII
            # and the rolling transitions surface recipient-level
            # activity, so we keep it ephemeral and gated.
            if not self._is_admin(interaction):
                await interaction.response.send_message(
                    "Admin only.", ephemeral=True,
                )
                return
            if self._engagement_score_db is None:
                await interaction.response.send_message(
                    "Engagement scoring not wired. Set RESEND_WEBHOOK_SECRET "
                    "and restart the bot so the events + scoring DB are "
                    "available.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                counts = await self._engagement_score_db.tier_counts()
                top = await self._engagement_score_db.top_engaged(limit=5)
                transitions = await self._engagement_score_db.recent_transitions(
                    limit=5,
                )
            except Exception:
                logger.exception("engagement-snapshot query crashed")
                await interaction.followup.send(
                    "Snapshot query crashed. Check logs.",
                    ephemeral=True,
                )
                return
            body = _render_engagement_snapshot(counts, top, transitions)
            await interaction.followup.send(body, ephemeral=True)

        # Sync guild commands once the client is ready. discord.Client (base
        # class, not commands.Bot) has no add_listener hook. wait_until_ready()
        # also refuses to run until client.login() has been called, which
        # happens inside listener.start() AFTER register() returns. So we poll
        # for readiness with short sleeps instead of relying on a single await.
        async def _deferred_sync() -> None:
            # Poll up to 60s waiting for client to finish login + become ready.
            for _ in range(120):
                if client.is_ready():
                    break
                await asyncio.sleep(0.5)
            else:
                logger.error(
                    "Discord client never became ready; slash commands not synced",
                )
                return
            try:
                await tree.sync(guild=guild)
                logger.info(
                    "Email slash commands synced to guild %d", self._guild_id,
                )
            except Exception:
                logger.exception("Failed to sync slash commands")

        asyncio.create_task(_deferred_sync(), name="slash_command_sync")

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        if not self._admin_ids:
            return False
        user_id = interaction.user.id if interaction.user else 0
        return user_id in self._admin_ids
