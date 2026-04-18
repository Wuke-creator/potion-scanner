"""Telegram bot command handlers for the verification flow.

Commands:

  /start    — welcome message + how to verify
  /verify   — issues a fresh state token + PKCE pair, stores the pending row,
              DMs the user a Discord OAuth sign-in link
  /settings — re-shows the channel subscription buttons
  /status   — shows whether the user is currently verified
  /help     — lists available commands

After verification succeeds, the OAuth callback sends the channel picker
message with inline keyboard buttons. Tapping a button toggles that channel
on/off and updates the button labels in-place.

Hooked into ``telegram.ext.Application`` via ``register_handlers()``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from html import escape as escape_html

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.analytics import AnalyticsDB, StatsWindow
from src.config import Config
from src.verification.db import VerificationDB
from src.verification.discord_oauth import DiscordOAuthClient, new_pkce_pair
from src.verification.state_token import issue as issue_state

logger = logging.getLogger(__name__)


_WELCOME = (
    "\U0001f52e *Potion Elite Signals*\n\n"
    "This bot forwards live trading calls from the Potion Discord "
    "straight to your DMs.\n\n"
    "\U0001f4c8 *Channels you can track:*\n"
    "  \u2022 Perp Bot Calls\n"
    "  \u2022 Manual Perp Calls\n"
    "  \u2022 Prediction Calls\n\n"
    "\U0001f510 To get started, you need the *Elite* role in the "
    "Potion Discord. Hit /verify to sign in and prove it.\n\n"
    "\U0001f514 *Pro tip:* make sure Telegram notifications are on for "
    "this chat so you never miss a call. Tap the bot name at the top "
    "of this chat and toggle Notifications on."
)

_HELP = (
    "\U0001f52e *Potion Elite Signals*\n\n"
    "/verify - sign in with Discord to confirm your Elite role\n"
    "/settings - choose which channels you get pinged on\n"
    "/data - 7d and 30d call counts + top PnL per channel\n"
    "/status - check your verification status\n"
    "/help - this message\n\n"
    "\u2753 Having issues? Head to Potion support:\n"
    "https://discord.com/channels/1260259552763580537/1285628366162231346"
)

def _relative_time(seconds_ago: int) -> str:
    """Format seconds-ago as a compact relative string (e.g. '3d ago', '2h ago')."""
    if seconds_ago < 60:
        return "just now"
    if seconds_ago < 3600:
        return f"{seconds_ago // 60}m ago"
    if seconds_ago < 86400:
        return f"{seconds_ago // 3600}h ago"
    return f"{seconds_ago // 86400}d ago"


# Callback data prefix for subscription toggle buttons
_CB_PREFIX = "sub:"

# Main menu button labels (shown in the persistent reply keyboard).
# Tapping sends the label as a plain message, routed by the MessageHandlers.
_BTN_VERIFY = "\U0001f510 Verify"
_BTN_SETTINGS = "\u2699\ufe0f Settings"
_BTN_DATA = "\U0001f4ca Data"
_BTN_STATUS = "\U0001f50d Status"
_BTN_HELP = "\u2753 Help"
_BTN_SUPPORT = "\U0001f6df Support"


def _build_main_menu() -> ReplyKeyboardMarkup:
    """Build the persistent 2-column main menu keyboard."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(_BTN_VERIFY), KeyboardButton(_BTN_SETTINGS)],
            [KeyboardButton(_BTN_DATA), KeyboardButton(_BTN_STATUS)],
            [KeyboardButton(_BTN_HELP), KeyboardButton(_BTN_SUPPORT)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _build_channel_keyboard(
    config: Config, active_subs: set[str],
) -> InlineKeyboardMarkup:
    """Build an inline keyboard with one toggle button per channel."""
    buttons = []
    for ch in config.discord.channels:
        is_on = ch.key in active_subs
        icon = "\u2705" if is_on else "\u274c"  # green check / red X
        label = f"{icon} {ch.name}"
        buttons.append([
            InlineKeyboardButton(text=label, callback_data=f"{_CB_PREFIX}{ch.key}")
        ])
    return InlineKeyboardMarkup(buttons)


class VerificationCommands:
    """Wires Telegram command handlers to the verification subsystem."""

    def __init__(
        self,
        config: Config,
        db: VerificationDB,
        oauth_client: DiscordOAuthClient,
        analytics: AnalyticsDB | None = None,
    ):
        self._config = config
        self._db = db
        self._oauth = oauth_client
        self._analytics = analytics

    def register(self, application: Application) -> None:
        application.add_handler(CommandHandler("start", self._cmd_start))
        application.add_handler(CommandHandler("help", self._cmd_help))
        application.add_handler(CommandHandler("verify", self._cmd_verify))
        application.add_handler(CommandHandler("settings", self._cmd_settings))
        application.add_handler(CommandHandler("data", self._cmd_data))
        application.add_handler(CommandHandler("status", self._cmd_status))

        # Main menu button taps arrive as plain text messages.
        # Exact-match filters route them to the same handlers as the commands.
        application.add_handler(
            MessageHandler(filters.TEXT & filters.Regex(f"^{_BTN_VERIFY}$"), self._cmd_verify)
        )
        application.add_handler(
            MessageHandler(filters.TEXT & filters.Regex(f"^{_BTN_SETTINGS}$"), self._cmd_settings)
        )
        application.add_handler(
            MessageHandler(filters.TEXT & filters.Regex(f"^{_BTN_DATA}$"), self._cmd_data)
        )
        application.add_handler(
            MessageHandler(filters.TEXT & filters.Regex(f"^{_BTN_STATUS}$"), self._cmd_status)
        )
        application.add_handler(
            MessageHandler(filters.TEXT & filters.Regex(f"^{_BTN_HELP}$"), self._cmd_help)
        )
        application.add_handler(
            MessageHandler(filters.TEXT & filters.Regex(f"^{_BTN_SUPPORT}$"), self._cmd_support)
        )

        application.add_handler(
            CallbackQueryHandler(self._cb_toggle, pattern=f"^{_CB_PREFIX}")
        )
        application.add_handler(
            CallbackQueryHandler(self._cb_mute, pattern=r"^mute:")
        )

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(
                _WELCOME,
                parse_mode="Markdown",
                reply_markup=_build_main_menu(),
            )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(
                _HELP,
                parse_mode="Markdown",
                reply_markup=_build_main_menu(),
            )

    async def _cmd_support(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(
                "\U0001f6df *Potion Support*\n\n"
                "Having issues? Head to the Potion support channel:\n"
                "https://discord.com/channels/1260259552763580537/1285628366162231346",
                parse_mode="Markdown",
                reply_markup=_build_main_menu(),
            )

    async def _cmd_verify(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not update.effective_message:
            return
        user_id = update.effective_user.id

        existing = await self._db.get_verified(user_id)
        if existing is not None and existing.is_active:
            await update.effective_message.reply_text(
                "You're already verified. Use /settings to choose which "
                "channels you get pinged on."
            )
            return

        # Rate limit: 1 verify attempt per 5 minutes per user
        cooldown_key = f"verify_cooldown:{user_id}"
        last_attempt = ctx.bot_data.get(cooldown_key, 0)
        now = __import__("time").time()
        if now - last_attempt < 300:
            remaining = int(300 - (now - last_attempt))
            await update.effective_message.reply_text(
                f"Please wait {remaining}s before trying /verify again."
            )
            return
        ctx.bot_data[cooldown_key] = now

        state = issue_state(user_id, self._config.oauth.state_secret)
        code_verifier, code_challenge = new_pkce_pair()
        await self._db.store_pending(
            state=state, telegram_user_id=user_id, code_verifier=code_verifier,
        )

        url = self._oauth.build_authorize_url(
            state=state,
            code_challenge=code_challenge,
            redirect_uri=self._config.oauth.redirect_uri,
        )
        minutes = self._config.verification.pending_ttl_seconds // 60
        await update.effective_message.reply_text(
            f'To verify your Elite role in the Potion Discord, '
            f'<a href="{url}">sign in with Discord</a>.\n\n'
            f'This link expires in {minutes} minutes.',
            parse_mode="HTML",
        )
        logger.info("Issued /verify link to telegram_user_id=%d", user_id)

    async def _cmd_settings(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not update.effective_message:
            return
        user_id = update.effective_user.id

        record = await self._db.get_verified(user_id)
        if record is None or not record.is_active:
            await update.effective_message.reply_text(
                "You need to verify first. Run /verify to get started."
            )
            return

        subs = await self._db.get_subscriptions(user_id)
        keyboard = _build_channel_keyboard(self._config, subs)
        await update.effective_message.reply_text(
            "\U0001f514 *Your notification settings*\n\n"
            "Tap a channel to turn it on or off:",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    async def _cmd_data(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not update.effective_message:
            return
        if self._analytics is None:
            await update.effective_message.reply_text(
                "Analytics not available yet. Try again once the bot has "
                "recorded some signals."
            )
            return

        channel_keys = self._config.discord.channel_keys()
        weekly = await self._analytics.stats_window(
            days=7, label="7d", channel_keys=channel_keys,
        )
        monthly = await self._analytics.stats_window(
            days=30, label="30d", channel_keys=channel_keys,
        )

        text = self._format_data_report(weekly, monthly)
        await update.effective_message.reply_text(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    def _format_data_report(
        self, weekly: StatsWindow, monthly: StatsWindow,
    ) -> str:
        """Build the /data HTML output, per-channel with display names."""
        lines: list[str] = []

        for window in (weekly, monthly):
            label = window.window_label
            lines.append(f"<b>\U0001f4c8 {label} Calls</b>")
            lines.append("")
            for ch in self._config.discord.channels:
                stats = window.per_channel.get(ch.key)
                count = stats.signal_count if stats else 0
                display = ch.display_name or ch.name
                lines.append(f"{escape_html(display)}: <b>{count}</b>")
            lines.append("")
            lines.append(f"<b>\U0001f4b0 {label} Top PnL</b>")
            lines.append("")
            for ch in self._config.discord.channels:
                stats = window.per_channel.get(ch.key)
                display = ch.display_name or ch.name
                if stats is None or stats.top_pnl is None:
                    lines.append(f"{escape_html(display)}: <i>no wins yet</i>")
                    continue
                top = stats.top_pnl
                rel = _relative_time(int(__import__("time").time()) - top.opened_at)
                lines.append(
                    f"{escape_html(display)}: "
                    f"<b>+{top.pnl_pct:.0f}%</b> on "
                    f"{escape_html(top.pair)} ({rel})"
                )
            lines.append("")

        return "\n".join(lines).rstrip()

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not update.effective_message:
            return
        user_id = update.effective_user.id

        record = await self._db.get_verified(user_id)
        if record is None:
            await update.effective_message.reply_text(
                "You are not verified. Run /verify to start."
            )
            return
        if not record.is_active:
            await update.effective_message.reply_text(
                "Your verification was revoked (Elite role no longer present). "
                "Re-activate Elite in the Potion Discord and run /verify again."
            )
            return

        subs = await self._db.get_subscriptions(user_id)
        if subs:
            channel_names = []
            for key in sorted(subs):
                ch = self._config.discord.channel_by_key(key)
                channel_names.append(ch.name if ch else key)
            sub_text = "\n".join(f"  \u2705 {n}" for n in channel_names)
        else:
            sub_text = "  \u274c None (use /settings to turn channels on)"
        verified_at = datetime.fromtimestamp(record.verified_at, tz=timezone.utc)
        checked_at = datetime.fromtimestamp(record.last_checked_at, tz=timezone.utc)
        await update.effective_message.reply_text(
            f"\U0001f510 *Status: VERIFIED*\n\n"
            f"*Channels:*\n{sub_text}\n\n"
            f"Verified: {verified_at:%Y-%m-%d %H:%M UTC}\n"
            f"Last checked: {checked_at:%Y-%m-%d %H:%M UTC}",
            parse_mode="Markdown",
        )

    async def _cb_toggle(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline keyboard button press — toggle a channel subscription."""
        query = update.callback_query
        if not query or not query.data or not update.effective_user:
            return
        await query.answer()

        user_id = update.effective_user.id
        channel_key = query.data.removeprefix(_CB_PREFIX)

        # Validate channel_key exists in config
        if self._config.discord.channel_by_key(channel_key) is None:
            return

        # Verify user is still active
        record = await self._db.get_verified(user_id)
        if record is None or not record.is_active:
            await query.edit_message_text("Your verification has expired. Run /verify again.")
            return

        now_subscribed = await self._db.toggle_subscription(user_id, channel_key)
        logger.info(
            "User %d toggled %s -> %s",
            user_id, channel_key, "ON" if now_subscribed else "OFF",
        )

        # Refresh the keyboard to show updated state
        subs = await self._db.get_subscriptions(user_id)
        keyboard = _build_channel_keyboard(self._config, subs)
        await query.edit_message_text(
            "\U0001f514 *Your notification settings*\n\n"
            "Tap a channel to turn it on or off:",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    async def _cb_mute(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle mute button tap on a signal alert."""
        query = update.callback_query
        if not query or not query.data or not update.effective_user:
            return

        user_id = update.effective_user.id
        token = query.data.removeprefix("mute:").upper().strip()
        if not token:
            await query.answer()
            return

        now_muted = await self._db.toggle_muted_token(user_id, token)
        if now_muted:
            await query.answer(f"{token} muted. You won't get alerts for {token} pairs.")
        else:
            await query.answer(f"{token} unmuted. You'll get alerts for {token} pairs again.")
        logger.info(
            "User %d toggled mute %s -> %s",
            user_id, token, "MUTED" if now_muted else "UNMUTED",
        )


async def send_channel_picker(
    bot, config: Config, db: VerificationDB, telegram_user_id: int,
) -> None:
    """Send the post-verification channel picker message.

    Called by the OAuth callback after a successful verification.
    """
    subs = await db.get_subscriptions(telegram_user_id)
    keyboard = _build_channel_keyboard(config, subs)
    await bot.send_message(
        chat_id=telegram_user_id,
        text=(
            "\u2728 *Good stuff on verifying!*\n\n"
            "Now you just need to select what channels you "
            "want to be pinged on \u2764\ufe0f\n\n"
            "Tap to toggle:"
        ),
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
