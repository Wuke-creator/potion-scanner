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
from src.email_bot.db import EmailDB, Subscriber
from src.email_bot.webhook import normalize_reason

logger = logging.getLogger(__name__)


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
    ):
        self._db = db
        self._guild_id = guild_id
        self._admin_ids = admin_user_ids
        self._default_rejoin = default_rejoin_url
        self._launch_broadcaster = launch_broadcaster
        self._whop_email_sync = whop_email_sync

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
        )
        async def broadcast_feature(
            interaction: discord.Interaction,
            title: str,
            description: str,
            include_email: bool = True,
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

            # Defer because broadcasting can take 30s+ for large audiences
            await interaction.response.defer(ephemeral=True, thinking=True)
            stats = await self._launch_broadcaster.broadcast(
                title=title,
                description=description,
                include_email=include_email,
            )
            await interaction.followup.send(
                f"Feature launch complete:\n"
                f"  DM: {stats.dm_sent}/{stats.dm_attempted} sent "
                f"(blocked {stats.dm_blocked}, failed {stats.dm_failed})\n"
                f"  Email: {stats.email_sent}/{stats.email_attempted} sent "
                f"(failed {stats.email_failed})\n"
                f"  Duration: {stats.duration_sec:.1f}s",
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
