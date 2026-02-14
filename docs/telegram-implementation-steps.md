# Telegram Bot — Step-by-Step Implementation Guide

Each step is a self-contained chunk. Complete one before starting the next. Every step lists exactly what gets built, what you (the human) need to do, and how to verify it works.

---

## Step 0: BotFather Setup

**Who**: You (manual)
**Time**: 5 minutes

### Actions
1. Open Telegram, search for `@BotFather`
2. Send `/newbot`
3. Name: `Potion Perps Bot` (or whatever you want users to see)
4. Username: `potion_perps_bot` (must end in `bot`, must be unique)
5. Copy the token BotFather gives you
6. Send `/setdescription` — "Automated Hyperliquid perpetual futures trading"
7. Send `/setabouttext` — "Invite-only automated trading bot for Hyperliquid"
8. Get your own Telegram user ID: search for `@userinfobot`, send it `/start`, copy your ID number

### Outcome
- You have a `TELEGRAM_BOT_TOKEN` (looks like `123456:ABC-DEF1234...`)
- You have your `TELEGRAM_ADMIN_IDS` (your numeric user ID)
- Add both to your `.env` file:
  ```
  TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234...
  TELEGRAM_ADMIN_IDS=your_user_id
  ```

### Verify
- Search for your bot username in Telegram — it should appear (won't respond yet)

---

## Step 1: Skeleton Bot + /start + /help

**Who**: Claude
**Files created**: `src/telegram/__init__.py`, `src/telegram/bot.py`, `src/telegram/handlers/__init__.py`, `src/telegram/handlers/help.py`
**Files modified**: `requirements.txt`, `main.py`

### What gets built
- `TelegramBot` class with `start()` and `stop()` methods
- `/start` command — sends welcome message
- `/help` command — lists available commands
- Integration into `main.py` — bot starts/stops with the rest of the system
- `python-telegram-bot` added to requirements

### Verify
1. `pip install -r requirements.txt`
2. `python main.py` (with `TELEGRAM_BOT_TOKEN` in `.env`)
3. Open Telegram, send `/start` to your bot — should reply with welcome message
4. Send `/help` — should list commands
5. `pytest` — all existing 291 tests still pass
6. Without `TELEGRAM_BOT_TOKEN` in env — bot disabled, rest of system works

---

## Step 2: Invite Code System (DB + Logic)

**Who**: Claude
**Files created**: `src/telegram/invite_codes.py`
**Files modified**: `src/state/user_db.py`

### What gets built
- `invite_codes` table in SQLite (code, created_by, duration_days, status, etc.)
- New columns on `user_config`: `telegram_chat_id`, `invite_code`, `access_expires_at`
- Code generation function: `PPB-XXXX-XXXX` format
- `UserDatabase` methods: `create_invite_code()`, `validate_invite_code()`, `redeem_invite_code()`, `list_invite_codes()`, `revoke_invite_code()`, `get_expired_users()`, `extend_user_access()`, `revoke_user_access()`, `set_telegram_chat_id()`, `get_telegram_chat_id()`, `get_all_telegram_chat_ids()`
- Tests for all invite code operations

### Verify
1. `pytest tests/test_user_db.py` — existing + new invite code tests pass
2. New tests cover: generate code, validate valid/invalid/redeemed, redeem sets expiry, revoke, list with filters, extend access, expired user detection

---

## Step 3: Admin Code Commands

**Who**: Claude
**Files created**: `src/telegram/handlers/admin.py`, `src/telegram/middleware.py`
**Files modified**: `src/telegram/bot.py`

### What gets built
- Admin middleware: checks if user's Telegram ID is in `TELEGRAM_ADMIN_IDS`
- `/generate_code [days]` — generates and replies with a code
- `/generate_codes <count> [days]` — batch generate
- `/list_codes` — shows all codes with status
- `/revoke_code <code>` — revokes an unused code
- Non-admins get "This command is only available to administrators"

### Verify
1. Send `/generate_code 30` from your admin account — get back a `PPB-XXXX-XXXX` code
2. Send `/generate_codes 3 30` — get 3 codes
3. Send `/list_codes` — see all codes listed with "active" status
4. Send `/revoke_code PPB-XXXX-XXXX` — code marked revoked
5. Send `/list_codes` again — revoked code shows as revoked
6. Have someone else (non-admin) try `/generate_code` — rejected
7. `pytest` — all tests pass

---

## Step 4: Registration Flow

**Who**: Claude
**Files created**: `src/telegram/handlers/registration.py`, `src/telegram/formatters.py`
**Files modified**: `src/telegram/bot.py`

### What gets built
- ConversationHandler with states: INVITE_CODE → ACCOUNT_ADDRESS → API_WALLET → API_SECRET → NETWORK
- Each credential message deleted immediately after reading
- Invite code validated before proceeding
- DM-only check (rejects registration in group chats)
- Credential validation against Hyperliquid API (testnet)
- User created in DB with encrypted credentials
- Pipeline activated via orchestrator
- `/cancel` exits registration at any step

### Who: You (manual testing)
- You need a Hyperliquid testnet account with API credentials to test the full flow
- If you don't have one yet, create one at app.hyperliquid.xyz (testnet)

### Verify
1. Send `/register` to bot
2. Bot asks for invite code
3. Enter a valid code from Step 3 — bot accepts, asks for account address
4. Enter account address — message deleted, bot asks for API wallet
5. Enter API wallet — message deleted, bot asks for private key
6. Enter private key — message deleted, bot shows network buttons
7. Click testnet — bot validates credentials, shows "Registration complete"
8. Check DB: user exists, credentials encrypted, code marked redeemed
9. Send `/register` again — "You're already registered"
10. Try with invalid code — rejected
11. Try with already-redeemed code — rejected
12. Send `/cancel` mid-registration — exits cleanly
13. `pytest` — all tests pass

---

## Step 5: Account Monitoring

**Who**: Claude
**Files created**: `src/telegram/handlers/account.py`, `src/telegram/keyboards.py`
**Files modified**: `src/telegram/bot.py`, `src/telegram/middleware.py`

### What gets built
- Auth middleware: registered users only (checks DB by Telegram chat_id)
- Expiry middleware: blocks commands if access expired
- `/balance` — shows USDC balance, account value, margin, withdrawable
- `/positions` — lists open positions with PnL
- `/status` — risk dashboard + access expiry date
- Inline keyboard navigation between views
- Formatters for currency, percentages, position display

### Verify
1. Send `/balance` — shows your testnet balance
2. Send `/positions` — shows open positions (or "No open positions")
3. Send `/status` — shows risk limits, exposure, and "Access expires: YYYY-MM-DD"
4. Unregistered user tries `/balance` — rejected with "Use /register first"
5. Click inline buttons to navigate between views
6. `pytest` — all tests pass

---

## Step 6: Configuration Menu

**Who**: Claude
**Files created**: `src/telegram/handlers/config.py`
**Files modified**: `src/telegram/bot.py`, `src/telegram/keyboards.py`

### What gets built
- `/config` — shows current settings with inline keyboard menu
- Strategy preset selection via buttons (6 presets)
- Auto-execute toggle button
- Risk limit adjustment (sends prompts for new values)
- Leverage adjustment
- `/preset <name>` — quick preset change via command
- `/auto` — quick toggle auto-execute
- All changes persist to DB immediately

### Verify
1. Send `/config` — see current settings
2. Click "Strategy" — see 6 preset buttons
3. Click "conservative" — config updated, confirmed
4. Send `/config` again — shows "conservative" as active preset
5. Click "Auto-Execute" — toggles on/off
6. Send `/preset runner` — changes back to runner
7. Send `/auto` — toggles auto-execute
8. Click "Risk Limits" — shows current limits, prompts to change
9. Enter new max_leverage value — saved to DB
10. `pytest` — all tests pass

---

## Step 7: Trade Views

**Who**: Claude
**Files created**: `src/telegram/handlers/trades.py`
**Files modified**: `src/telegram/bot.py`, `src/telegram/formatters.py`

### What gets built
- `/trades` — list active trades (pending + open) with details
- `/history` — list recently closed trades with P&L
- `/stats` — win rate, total trades, average profit, best/worst trade
- Trade detail view via inline button (all TPs, SL, orders, timestamps)
- Pagination for long lists (> 5 trades per page)

### Verify
1. Send `/trades` — shows active trades (or "No active trades")
2. Send `/history` — shows closed trades
3. Send `/stats` — shows performance summary
4. If trades exist, click a trade — see full detail view
5. `pytest` — all tests pass

**Note**: To test with real trades, you'll need to run the bot with the simulation adapter sending sample signals while registered on testnet.

---

## Step 8: Trade Notifications

**Who**: Claude
**Files created**: `src/telegram/notifications.py`
**Files modified**: `src/pipeline.py`, `src/orchestrator.py`

### What gets built
- `TelegramNotifier` class with methods for each event type
- Pipeline gains optional `notifier` parameter
- Orchestrator passes notifier to Pipeline during `activate_user()`
- Notifications sent for: new signal, trade opened, TP hit, stop hit, trade closed, breakeven, risk warning
- When `auto_execute=false`: new signal notification includes Approve/Reject buttons

### Verify
1. Run bot with simulation adapter (`adapter=simulation` in config)
2. Sample signals process through pipeline
3. Check Telegram — you should receive notifications for each trade event
4. If `auto_execute=false`: signal notification shows Approve/Reject buttons
5. Click Approve — trade submitted, get "Trade opened" notification
6. Click Reject — trade canceled
7. `pytest` — all tests pass

---

## Step 9: Trade Approval Flow

**Who**: Claude
**Files modified**: `src/telegram/handlers/trades.py`, `src/telegram/notifications.py`

### What gets built
- Approve button callback: submits the pending trade to exchange
- Reject button callback: cancels the trade
- Close Position button: market-closes an open trade
- Confirmation dialog for Close Position (are you sure?)
- Updates notification message after action (removes buttons, adds status)

### Verify
1. Set `auto_execute=false` in config
2. Send a signal through simulation adapter
3. Notification arrives with Approve/Reject buttons
4. Click Approve — trade opens, buttons replaced with "Approved"
5. Repeat, click Reject — trade canceled, buttons replaced with "Rejected"
6. On an open trade detail, click Close Position — confirmation dialog
7. Confirm — position closed on exchange
8. `pytest` — all tests pass

---

## Step 10: Admin User Management

**Who**: Claude
**Files modified**: `src/telegram/handlers/admin.py`

### What gets built
- `/users` — list all users with status, preset, trade count, expiry date
- `/extend <user_id> <days>` — extend access
- `/revoke <user_id>` — revoke access, deactivate pipeline
- `/kill` — emergency kill switch with confirmation button
- `/resume` — resume after kill
- `/broadcast <message>` — send message to all active users

### Verify
1. Send `/users` — see all registered users
2. Send `/extend <your_id> 30` — access extended by 30 days
3. Send `/kill` — confirmation dialog appears
4. Confirm kill — all positions closed, "killed" status confirmed
5. Send `/resume` — trading resumed
6. Send `/broadcast Hello everyone` — all active users receive the message
7. `pytest` — all tests pass

---

## Step 11: Expiry Enforcement

**Who**: Claude
**Files modified**: `src/telegram/bot.py`, `src/telegram/middleware.py`, `src/telegram/notifications.py`

### What gets built
- Background task (runs every hour): checks for expired users
- Expired users: pipeline deactivated, status set to inactive, Telegram notification sent
- Expiry warning notifications: 3 days before, 1 day before
- All commands blocked for expired users with "Access expired" message
- Admin `/extend` re-activates expired users

### Verify
1. Create a test code with 1-minute duration (for testing): manually set `access_expires_at` in DB to 1 minute from now
2. Wait for expiry — user gets notification, pipeline deactivated
3. Try `/balance` as expired user — blocked with expiry message
4. Admin sends `/extend <user_id> 30` — user re-activated
5. User can use commands again
6. `pytest` — all tests pass

---

## Step 12: Final Polish + BotFather Commands

**Who**: Claude (code) + You (BotFather)

### What Claude builds
- Error handling: graceful messages for exchange errors, rate limits, invalid input
- `/cancel` works from any state
- Unknown command handler — friendly "try /help" message
- Group chat protection — bot only responds in DMs
- Rate limiting — max 30 commands per minute per user
- All tests finalized

### What you do (BotFather)
Send `/setcommands` to BotFather and paste:
```
start - Welcome and info
register - Register with invite code
help - Show all commands
balance - Account balance
positions - Open positions
trades - Active trades
history - Trade history
stats - Trading statistics
config - Configure settings
preset - Change strategy preset
auto - Toggle auto-execute
status - Risk dashboard
activate - Activate trading
deactivate - Pause trading
cancel - Cancel current action
```

### Verify
1. Full end-to-end test:
   - Generate code → register with code → check balance → change config → receive trade notification → approve trade → view trade → check history
2. Edge cases: invalid input at every step handled gracefully
3. Group chat: bot ignores messages
4. Rate limit: rapid-fire commands eventually throttled
5. `pytest` — all tests pass (291 existing + new Telegram tests)
6. Command autocomplete works in Telegram (from BotFather setup)

---

## Summary: What You Do vs What Claude Does

| Step | Claude | You |
|------|--------|-----|
| 0 | — | Create bot in BotFather, get token + your user ID, add to `.env` |
| 1 | Build skeleton bot, /start, /help | `pip install`, run bot, test /start |
| 2 | Invite code DB + logic | Review tests |
| 3 | Admin code commands | Test /generate_code in Telegram |
| 4 | Registration flow | Test full registration with testnet credentials |
| 5 | Account monitoring | Test /balance, /positions, /status |
| 6 | Config menu | Test preset changes, toggles |
| 7 | Trade views | Test /trades, /history, /stats |
| 8 | Trade notifications | Run simulation adapter, watch notifications arrive |
| 9 | Trade approval flow | Test Approve/Reject/Close buttons |
| 10 | Admin user management | Test /users, /kill, /resume, /broadcast |
| 11 | Expiry enforcement | Test with short-duration code |
| 12 | Polish + error handling | Set commands in BotFather, full e2e test |
