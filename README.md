# Potion Discord -> Telegram Signals Bot

Forwards Potion Discord trading calls (perps + manual perps + spot/memecoin
predictions) as **direct messages** to every verified Elite member, with
the correct referral link attached so they can click through and trade in
one tap.

Access is gated by **Whop OAuth**: a user runs `/verify` in the bot, signs
in to Whop, and if they hold an active Elite membership in the Potion Whop
business the bot starts DMing them every new trading call automatically.
A 24h re-verification cron checks every active user against Whop and
auto-revokes anyone whose membership lapsed. No group, no invite link —
every message is a private DM, just like the Padre bot.

## Scale (verified via stress test)

| Users | Alerts | Duration  | Delivery | Notes |
|------:|-------:|----------:|---------:|-------|
|   500 |      3 |     58s   |  100.0%  | Per-alert fan-out: ~20s |
|  1000 |      3 |    118s   |  100.0%  | Per-alert fan-out: ~40s |
|  2000 |      3 |    238s   |  100.0%  | Per-alert fan-out: ~80s |
|  1000 |      3 |    109s   |   95.6%  | Chaos: 5% blocked + 2% transient errors + 1% rate-limited |

Throughput holds at 25 msgs/sec under all scenarios, matching the
configured rate cap. The dispatcher auto-marks users who blocked the bot
as inactive after their first failed delivery, so they're skipped on every
subsequent alert without any manual intervention.

Run `python -m scripts.stress_test --users 1000 --alerts 3` to reproduce.

## What it does

1. A Discord bot listens on three Potion channels:
   - **Perp Bot Calls** (1445440392509132850) - structured Potion Perps Bot signals
   - **Manual Perp Calls** (1316518499283370064) - manually-posted perps trades
   - **Prediction Calls** (1420272690459181118) - spot / memecoin predictions
2. Each new message is classified by the parser:
   - Structured `TRADING SIGNAL ALERT` -> all 12 fields extracted, formatted neatly
   - Lifecycle events (`TP HIT`, `BREAKEVEN`, `STOP HIT`, etc.) -> forwarded with a label
   - `NOISE` and free-form messages on memecoin channels -> forwarded verbatim
   - `PREPARATION` teasers -> dropped
3. The router attaches the right referral link based on the source channel:
   - Perp channels -> `REF_LINK_PERPS` (default: https://partner.blofin.com/d/potion)
   - Memecoin channel -> `REF_LINK_MEMECOIN` (default: https://trade.padre.gg/rk/orangie)
4. The **Dispatcher** fans the formatted alert out as a direct message
   to every active verified user, rate-limited at 25 msgs/sec to stay
   under Telegram's global bot API limit. Blocked users are auto-marked
   inactive. RetryAfter is replayed. Back-to-back alerts queue up cleanly.

A Telegram bot handles Whop verification:

- `/start` - welcome
- `/verify` - DMs the user a Whop OAuth sign-in URL with PKCE + signed state
- Whop redirects to `/oauth/whop/callback` (aiohttp server)
- Callback exchanges the code, calls `/v5/me/memberships`, looks for an
  active Elite membership in the Potion company
- **On success**: stores an encrypted refresh token, flips the user to
  active — from then on the dispatcher includes them in every alert fan-out
- **On failure**: DM the user a denial pointing to the Elite signup URL
- `/status` - shows verification state + last re-check timestamp
- 24h reverify cron walks every active user, refreshes their Whop access
  token, re-checks the membership, and flips `is_active = 0` for anyone
  who's lapsed (the dispatcher then skips them on every future alert)

## Architecture

```
+---------------------+    asyncio.Queue     +-------------+
|  Discord listener   | -------------------> |   Router    |
|  (multi-channel)    |                      +------+------+
+---------------------+                             |
                                                    v
                                          +---------------------+
                                          |    Dispatcher       |
                                          |  queue + workers    |
                                          |  token-bucket 25/s  |
                                          +----------+----------+
                                                     |
                          list active users ---------+---------> Telegram DMs
                                                                 (one per user,
                                                                  Markdown + link)

+--------------------+   +-------------+   +-------------------+
| Telegram bot       |   |  aiohttp    |   |  24h reverify     |
| /start /verify     |   |  /oauth/    |   |  cron task        |
| /status /help      |   |  whop/      |   |                   |
+--------------------+   |  callback   |   +-------------------+
         |               +-------------+             |
         | new pending         |                     |
         v                     v                     v
       +-------------------------------+
       |   aiosqlite verified.db       |
       |   verified_users is_active    |
       |   + pending_verifications     |
       +-------------------------------+
```

All components live inside one process, sharing a single asyncio event
loop. There's no IPC, no Redis, no message broker - all queues are
in-memory. The Dispatcher reads from the same SQLite DB that verification
writes to, so an alert dispatched in the middle of a verification flow
automatically includes any user who just finished `/verify`.

## File layout

```
.
|-- main.py                    # entry point: wires everything together
|-- src/
|   |-- config/settings.py     # YAML + env config loader
|   |-- discord_listener.py    # multi-channel discord.py client
|   |-- formatter.py           # Telegram alert templates (pure functions)
|   |-- rate_limiter.py        # AsyncTokenBucket (Telegram rate limit)
|   |-- dispatcher.py          # DM fan-out: queue + workers + rate limit
|   |-- router.py              # classify + parse + format dispatch
|   |-- parser/                # Potion Perps Bot message parser (10 types)
|   |-- verification/
|   |   |-- state_token.py     # HMAC-signed OAuth state
|   |   |-- db.py              # aiosqlite store
|   |   |-- whop_oauth.py      # PKCE, token exchange, memberships check
|   |   |-- oauth_callback.py  # aiohttp /oauth/whop/callback server
|   |   |-- commands.py        # Telegram /start /verify /status /help
|   |   |-- reverify_job.py    # 24h re-check + revoke cron
|   |   `-- runtime.py         # lifecycle handle
|   `-- crypto.py              # Fernet helper used by verification
|-- config/config.yaml         # channel routing + non-secret settings
|-- signals/samples/           # 28 real Potion Discord messages (test fixtures)
|-- scripts/stress_test.py     # stress harness (simulate N users x K alerts)
|-- tests/                     # 156+ tests covering parser, router, dispatcher,
|                              #   rate limiter, verification db, state token, ...
|-- Dockerfile
|-- docker-compose.yml
|-- requirements.txt
`-- .env.example
```

## Setup

### 1. Discord bot

1. Go to https://discord.com/developers/applications and create a new
   application.
2. Add a bot user, copy its token into `DISCORD_BOT_TOKEN`.
3. Under **Privileged Gateway Intents**, enable **Message Content Intent**.
4. Generate an OAuth2 invite URL with the `bot` scope and `Read Messages`,
   `Read Message History`, `View Channels` permissions for the trading
   channels you need.
5. Have a Potion admin invite the bot to the Potion server.

### 2. Telegram bot

1. Talk to `@BotFather` on Telegram, run `/newbot`, copy the token into
   `TELEGRAM_BOT_TOKEN`.
2. That's it — the bot DMs each verified user directly, no group needed.
   The user must `/start` the bot once to open a chat with it before the
   bot can send them anything.

### 3. Whop OAuth

1. Visit https://whop.com/dashboard/biz_pn9Fq67uJNjgj1/ and find the
   Developer / API section.
2. Create an OAuth application.
3. Add the redirect URI exactly as it appears in `OAUTH_REDIRECT_URI`
   (e.g. `https://verify.example.com/oauth/whop/callback`).
4. Copy `WHOP_CLIENT_ID` and `WHOP_CLIENT_SECRET` into `.env`.
5. (Optional) If Potion sells multiple Whop tiers, find the Elite product
   ID and put it in `WHOP_ELITE_PRODUCT_ID`. Otherwise leave empty - any
   active membership in the company counts as Elite.

### 4. Generate secrets

```bash
python -c "import secrets; print(secrets.token_hex(32))"
# -> paste into OAUTH_STATE_SECRET

python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# -> paste into WHOP_REFRESH_TOKEN_ENCRYPTION_KEY
```

### 5. Public OAuth callback

The aiohttp callback server needs to be reachable from `api.whop.com`.
For local development use ngrok or cloudflared:

```bash
ngrok http 8080
# copy the https://abc123.ngrok.io URL into OAUTH_REDIRECT_URI as
# https://abc123.ngrok.io/oauth/whop/callback
# also register that exact URL in the Whop OAuth app settings
```

For production, point a real domain (Caddy, Cloudflare Tunnel, nginx) at
the container's port 8080.

## Running

### Local Python

```bash
pip install -r requirements.txt
cp .env.example .env
# fill in .env
python main.py
```

### Docker

```bash
cp .env.example .env
# fill in .env
docker compose up --build
```

### Tests

```bash
pip install -r requirements.txt
python -m pytest tests/
```

There are **156+ tests** covering the parser, classifier, formatter,
router, dispatcher, rate limiter, config loader, state token,
verification DB, and Whop OAuth URL building. Network-bound Whop calls
and Telegram sends are mocked.

### Stress test

```bash
python -m scripts.stress_test --users 1000 --alerts 3 --rate 25
python -m scripts.stress_test --users 2000 --alerts 3 --rate 25
python -m scripts.stress_test --users 1000 --alerts 3 --rate 25 \
    --block-rate 0.05 --failure-rate 0.02 --retry-after-rate 0.01
```

Simulates N fake verified users in an in-memory DB + K back-to-back
alerts against a fake Telegram bot with configurable failure injection.
Prints per-alert stats, aggregate throughput, and a pass/fail verdict
against a throughput floor.

## How verification controls access

No group, no invite link — access control is enforced at the dispatcher
level via the `is_active` flag on each verified user row.

- `/verify` → Whop OAuth → `/me/memberships` → if an active Elite
  membership is found in the Potion company, insert a `verified_users`
  row with `is_active = 1`. From the next alert onward the user is
  automatically included in every fan-out.
- **Denial**: if the user isn't Elite, the bot DMs them a denial pointing
  to `WHOP_ELITE_SIGNUP_URL`. No DB row written.
- **24h reverify cron**: refreshes each user's Whop access token, re-checks
  `/me/memberships`, and flips `is_active = 0` for anyone whose membership
  has lapsed. The dispatcher filters on `is_active = 1` so lapsed users
  stop receiving DMs immediately.
- **Blocked users**: if a user blocks the bot on Telegram, the dispatcher
  catches the `Forbidden` error and auto-marks them inactive so we don't
  waste sends on them going forward.
- Refresh tokens are encrypted at rest with Fernet (key in
  `WHOP_REFRESH_TOKEN_ENCRYPTION_KEY`).

## Customization

| Want to... | Edit |
|---|---|
| Add another channel | `config/config.yaml` `discord.channels`, then add the env var |
| Change the alert template | `src/formatter.py` |
| Change classification rules | `src/parser/classifier.py` |
| Adjust reverify frequency | `config/config.yaml` `verification.reverify_interval_seconds` |
| Change the welcome message | `src/verification/commands.py` `_WELCOME` |

## Out of scope (for v1)

- No trade execution (the bot only forwards calls, never trades)
- No analytics or trade outcome tracking
- No per-user routing within Telegram (one shared Elite group)
- No webhook ingestion (Discord gateway only)
- No DEX swap quoting
