# Telegram Bot for Potion Perps Bot

## Context

The multi-user trading infrastructure is complete (UserDatabase, Orchestrator, Admin API, encrypted credentials, per-user pipelines). The next step is a user-facing Telegram bot that lets users register, configure their strategy, monitor trades, and receive real-time notifications — all without touching the Admin REST API directly.

**Why Telegram first**: Access to the Discord signal server requires demonstrating a working bot to the boss. The Telegram bot is the user-facing MVP that proves the system works end-to-end.

**Architecture**: The bot runs in the same asyncio event loop as the existing services. It calls UserDatabase and Orchestrator directly (no HTTP), same as the Admin API does.

---

## File Structure

```
src/telegram/
├── __init__.py
├── bot.py                    # TelegramBot class — init, start, stop
├── handlers/
│   ├── __init__.py
│   ├── registration.py       # Multi-step credential collection
│   ├── account.py            # /balance, /positions, /status
│   ├── config.py             # /config, /preset, /auto
│   ├── trades.py             # /trades, /history, /stats, approve/reject
│   ├── admin.py              # /users, /kill, /resume (admin-only)
│   └── help.py               # /start, /help
├── keyboards.py              # All InlineKeyboardMarkup builders
├── notifications.py          # TelegramNotifier — push trade events to users
├── middleware.py              # Auth check (registered?), admin check, DM-only check
└── formatters.py             # Message formatting (balance, positions, trades, etc.)

tests/telegram/
├── __init__.py
├── test_formatters.py
├── test_keyboards.py
├── test_registration.py
├── test_account.py
├── test_config.py
├── test_trades.py
├── test_admin.py
└── test_notifications.py
```

**Files to modify:**
- `main.py` — add TelegramBot startup/shutdown
- `src/state/user_db.py` — add `telegram_chat_id` column to user_config table
- `src/pipeline.py` — add optional notifier hook for trade events
- `requirements.txt` — add `python-telegram-bot>=20.7`
- `docker-compose.yml` — add `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_IDS` env vars

---

## Features by Category

### User Management
- **Registration**: Multi-step ConversationHandler collects credentials (account_address, api_wallet, api_secret, network)
- **Security**: Credentials collected in DM only; user messages deleted immediately after reading
- **Credential validation**: Test credentials against Hyperliquid API before saving
- **Activate / Deactivate**: User can pause/resume their pipeline

### Account Monitoring
- `/balance` — USDC balance, account value, margin used, withdrawable
- `/positions` — Open positions with entry, current price, unrealized PnL, leverage, liquidation price
- `/status` — Risk dashboard: exposure vs limits, positions open vs max, daily P&L vs circuit breaker

### Trade Management
- `/trades` — Active trades (pending + open) with details
- `/history` — Recent closed trades with P&L
- `/stats` — Win rate, total P&L, average profit, trade count
- **Approve/Reject** — When `auto_execute=false`, new signals show inline buttons to approve or reject

### Configuration
- `/config` — View all current settings with inline keyboard menu
- **Preset selection** — Switch between 6 built-in presets via buttons
- **Auto-execute toggle** — One-tap on/off
- **Risk limits** — Adjust max_open_positions, max_daily_loss_pct, max_position_size_usd, max_total_exposure_usd
- **Leverage** — Adjust max_leverage (validated 1–50)
- **Position sizing** — Modify size_by_risk multipliers per risk level

### Notifications (Push)
- New signal received (with approve/reject buttons if auto_execute=off)
- Trade opened on exchange
- TP hit (TP1/TP2/TP3 with profit %)
- Stop loss hit
- Trade fully closed (with final P&L summary)
- SL moved to breakeven
- Risk warning (approaching limits)
- System events (kill switch activated, bot started/stopped)

### Admin Commands (admin-only)
- `/users` — List all registered users with status
- `/kill` — Emergency kill switch with confirmation dialog
- `/resume` — Resume after kill
- `/broadcast <message>` — Send message to all active users
- Force activate/deactivate any user

---

## Key Conversation Flows

### Registration (`/register`)

```
User: /register
Bot: "I'll need your Hyperliquid API credentials. Make sure you're in a private chat."
     "Send your Account Address (0x...):"

User: 0x1234...
Bot: [deletes user's message]
     "Account Address saved (ending ...4567). Now send your API Wallet Address:"

User: 0xabcd...
Bot: [deletes user's message]
     "API Wallet saved. Now send your API Private Key:"

User: 0xsecret...
Bot: [deletes user's message]
     "Private Key encrypted."
     [testnet] [mainnet]

User: [clicks testnet]
Bot: "Validating credentials..."
     "Registration complete! Active with default settings:"
     "- Strategy: runner (33/33/34)"
     "- Auto-execute: OFF"
     "- Max leverage: 20x"
     [View Balance] [Configure Settings] [Help]
```

### Trade Approval (auto_execute=false)

```
Bot: "NEW SIGNAL - Trade #42"
     "BTC/USDT LONG"
     "Entry: $42,350 | SL: $41,500 (-2.0%)"
     "TP1: $43,000 | TP2: $43,500 | TP3: $44,200"
     "Risk: MEDIUM | Leverage: 20x | Size: $100"
     [Approve] [Reject]

User: [clicks Approve]
Bot: "Trade #42 submitted..."
Bot: "TRADE OPENED - BTC/USDT LONG at $42,350"
     [View Details] [Close Position]
```

### Config Change

```
User: /config
Bot: "Current Config"
     "Strategy: runner | Auto: OFF | Leverage: 20x"
     [Strategy] [Auto-Execute] [Risk Limits] [Leverage]

User: [clicks Strategy]
Bot: "Select preset:"
     [runner] [conservative] [tp2_exit]
     [tp3_hold] [breakeven_filter] [small_runner]

User: [clicks conservative]
Bot: "Preset changed to conservative (100% exit at TP1)"
```

---

## Commands

| Command | Description | Access |
|---------|-------------|--------|
| `/start` | Welcome message | All |
| `/register` | Begin registration | Unregistered |
| `/help` | Show all commands | All |
| `/balance` | Account balance | Registered |
| `/positions` | Open positions | Registered |
| `/trades` | Active trades | Registered |
| `/history` | Closed trade history | Registered |
| `/stats` | Trading statistics | Registered |
| `/config` | View/change settings | Registered |
| `/preset <name>` | Quick preset change | Registered |
| `/auto` | Toggle auto-execute | Registered |
| `/status` | Risk dashboard | Registered |
| `/activate` | Activate pipeline | Registered |
| `/deactivate` | Deactivate pipeline | Registered |
| `/cancel` | Cancel current operation | All |
| `/users` | List all users | Admin |
| `/kill` | Emergency kill switch | Admin |
| `/resume` | Resume after kill | Admin |
| `/broadcast <msg>` | Message all users | Admin |

---

## Integration Architecture

### Direct Python calls (no HTTP)

```python
class TelegramBot:
    def __init__(self, user_db, orchestrator, global_config, admin_user_ids, bot_token):
        self._user_db = user_db          # UserDatabase instance
        self._orchestrator = orchestrator  # Orchestrator instance
        self._global_config = global_config
        self._admin_user_ids = admin_user_ids
```

All handlers call `self._user_db.*` and `self._orchestrator.*` directly. For per-user exchange data:
```python
ctx = self._orchestrator.pipelines.get(user_id)
if ctx:
    balance = ctx.client.get_balance()
    positions = ctx.client.get_open_positions()
    trades = ctx.db.get_open_trades()
```

### User ID mapping

Telegram `chat_id` (int) is used as `user_id` (str) throughout the system. Stored in user_config table as `telegram_chat_id` for reverse lookup (notifications).

### Notification hook in Pipeline

```python
# Pipeline.__init__ gains optional notifier parameter
# After trade events, call notifier.notify_* if set
# Orchestrator passes notifier to Pipeline during activate_user()
```

### main.py integration

```python
# After orchestrator.start() and admin_api.start():
if os.getenv("TELEGRAM_BOT_TOKEN"):
    telegram_bot = TelegramBot(user_db, orchestrator, config, admin_ids, token)
    await telegram_bot.start()
# In shutdown: await telegram_bot.stop()
```

---

## DB Changes

Add to `user_config` table in `src/state/user_db.py`:
```sql
telegram_chat_id INTEGER  -- Telegram chat ID for notifications
```

New methods:
- `set_telegram_chat_id(user_id, chat_id)`
- `get_telegram_chat_id(user_id) -> int | None`
- `get_all_telegram_chat_ids() -> dict[str, int]` (for broadcast)

---

## New Env Vars

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `TELEGRAM_BOT_TOKEN` | No | — | Token from @BotFather. Bot disabled if unset |
| `TELEGRAM_ADMIN_IDS` | No | — | Comma-separated Telegram user IDs for admin access |

---

## Testing

- **Unit tests**: Formatters (balance/position/trade text), keyboard builders, validators
- **Handler tests**: Mock `Update`, `CallbackContext`, `UserDatabase`, `Orchestrator`; verify correct messages sent, DB calls made, state transitions
- **Notification tests**: Mock `Bot.send_message`, verify correct chat_id and message format
- All tests use `python-telegram-bot`'s mock patterns; no real Telegram server needed
- Reuse existing test patterns (MagicMock, AsyncMock, tmpdir fixtures)

---

## Implementation Order

1. **Core infrastructure** — `bot.py`, middleware, `main.py` integration, `/start`, `/help`
2. **Registration flow** — ConversationHandler, credential validation, DB storage, message deletion
3. **Account monitoring** — `/balance`, `/positions`, `/status`, formatters
4. **Configuration** — `/config` menu, preset selection, risk limits, inline keyboards
5. **Trade management** — `/trades`, `/history`, `/stats`, approve/reject flow
6. **Notifications** — TelegramNotifier, Pipeline hook, push events
7. **Admin commands** — `/users`, `/kill`, `/resume`, `/broadcast`
8. **Tests** — Unit + integration for all handlers

---

## Verification

1. `pytest` — all existing 291 + new Telegram tests pass
2. Set `TELEGRAM_BOT_TOKEN` to a test bot — `/start` responds
3. `/register` collects credentials, validates against testnet, creates user in DB
4. `/balance` shows real testnet balance
5. Send a sample signal via simulation adapter — notification appears in Telegram
6. `/config` — change preset — verify DB updated
7. `auto_execute=false` — signal shows approve/reject buttons — approve opens position
8. Admin: `/kill` — all positions closed, `/resume` — signals process again
9. Without `TELEGRAM_BOT_TOKEN` — bot disabled, rest of system works normally
