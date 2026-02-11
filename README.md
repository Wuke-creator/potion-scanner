# Potion Perps Bot

Automated trading pipeline that takes signals from the Potion Perps Discord bot, parses them in real-time, and executes perpetual futures trades on Hyperliquid with configurable strategy presets and risk controls.

**Status:** Phase 2 complete — fully functional on testnet. End-to-end verified: signal ingestion, order execution, lifecycle management, position sync, and risk guardrails all working.

---

## How It Works

```
Discord Signal → Input Adapter → Classifier → Parser → Risk Gate → Position Sizer
    → Order Builder → Position Manager → Hyperliquid API
                                              ↓
                                        SQLite Database
```

1. A signal arrives (from CLI, file replay, or Discord)
2. The **classifier** identifies the message type (new signal, TP hit, SL hit, cancel, etc.)
3. The **parser** extracts structured data into typed dataclasses
4. For new signals:
   - **Risk gate** checks: max positions, daily loss limit, total exposure cap
   - **Position sizer** calculates USD allocation based on balance, risk level, and preset
   - **Order builder** creates entry + SL + TP orders using real exchange metadata
   - **Position manager** submits to Hyperliquid and records everything in SQLite
5. For lifecycle events (TP hit, stop hit, cancel, SL update):
   - The pipeline updates orders on the exchange and DB state accordingly

---

## Quick Start

```bash
# Install
pip install -r requirements.txt
cp .env.example .env
cp config/config.example.yaml config/config.yaml

# Edit .env with your Hyperliquid credentials
# Edit config/config.yaml with your strategy preferences

# Run (simulation mode — replays sample signals)
python main.py

# Run with a specific user ID
python main.py alice

# Run tests
python -m pytest tests/ -v
```

### Credentials (.env)

```
HL_ACCOUNT_ADDRESS=0x_your_master_account_address
HL_API_WALLET=0x_your_api_wallet_address
HL_API_SECRET=0x_your_api_wallet_private_key
```

Hyperliquid uses a **master account + API wallet** architecture:
- **Master account** — owns the funds, used for all queries
- **API wallet** — only signs transactions on behalf of the master account

---

## Strategy Presets

A "strategy" is just 3 settings — not a separate algorithm:

| Setting | What it controls |
|---------|-----------------|
| `tp_split` | How much to close at each TP level (e.g. [0.33, 0.33, 0.34]) |
| `move_sl_to_breakeven_after` | When to move SL to entry price (`tp1`, `tp2`, or `never`) |
| `size_pct` | % of account balance per trade |

### Built-in Presets

| Preset | TP Split | SL to BE | Size % | Description |
|--------|----------|----------|--------|-------------|
| `runner` | 33/33/34 | After TP1 | 2% | Default — let winners run |
| `conservative` | 100/0/0 | Never | 2% | Close everything at TP1 |
| `tp2_exit` | 50/50/0 | After TP1 | 2% | Exit fully at TP2 |
| `tp3_hold` | 0/0/100 | After TP1 | 2% | Hold everything for TP3 |
| `breakeven_filter` | 33/33/34 | After TP1 | 1.5% | Smaller size, same split |
| `small_runner` | 33/33/34 | After TP1 | 0.5% | Minimal risk runner |

### Custom Presets (config.yaml)

```yaml
strategy_presets:
  my_strategy:
    tp_split: [0.5, 0.3, 0.2]
    move_sl_to_breakeven_after: tp1
    size_pct: 1.5

strategy:
  active_preset: my_strategy
  auto_execute: true
```

---

## Risk Controls

All enforced automatically before every trade:

| Guard | Config Key | Default | What it does |
|-------|-----------|---------|-------------|
| Max open positions | `risk.max_open_positions` | 10 | Rejects new trades when limit reached |
| Daily loss circuit breaker | `risk.max_daily_loss_pct` | 10% | Stops all trading if cumulative daily losses exceed threshold |
| Total exposure cap | `risk.max_total_exposure_usd` | $2,000 | Caps combined USD across all open positions |
| Max position size | `risk.max_position_size_usd` | $500 | Per-trade USD cap |
| Min order value | `risk.min_order_usd` | $10 | Hyperliquid's minimum notional |
| Leverage cap | `strategy.max_leverage` | 20x | Overrides signal leverage (also capped by exchange per-asset max) |

---

## Supported Message Types

The parser handles all 10 message types from the Potion Perps Discord bot:

| Type | Action | Example |
|------|--------|---------|
| `SIGNAL_ALERT` | Parse, size, build orders, execute | New trade signal with entry/SL/TP |
| `TP_HIT` | Log, optionally move SL to breakeven | "TP TARGET 1 HIT" |
| `ALL_TP_HIT` | Mark trade closed with profit | "ALL TAKE-PROFIT TARGETS HIT" |
| `BREAKEVEN` | Move SL to entry price | "BREAK EVEN HIT AFTER TP1" |
| `STOP_HIT` | Mark trade closed with loss | "STOP TARGET HIT" |
| `CANCELED` | Cancel orders, close position | "Trade #1268 Canceled" |
| `TRADE_CLOSED` | Market-close remaining position | "TRADE CLOSED OUT" |
| `PREPARATION` | Log only — do NOT execute | "Trade #1284 Incoming..." |
| `MANUAL_UPDATE` | Detect SL moves, otherwise log | "Move SL to 1985" or free-text |
| `NOISE` | Ignore | Bot pings, announcements |

### Dynamic SL Adjustment

Manual update messages are parsed for SL-move instructions:
- "Move SL to 1985"
- "Adjust stop loss to 0.025"
- "New SL: 510.5"
- "SL → 0.178"

If detected, the bot cancels the old SL order and places a new one at the specified price.

---

## Symbol Mapping

110+ pairs mapped from Potion Perps format to Hyperliquid format:

| Pattern | Example | Handling |
|---------|---------|----------|
| Direct 1:1 | ETH/USDT → ETH | Strip /USDT |
| Kilo-prefix | 1000BONK/USDT → kBONK | Convert 1000X to kX |
| Bare meme coins | BONK/USDT → kBONK | Override table |
| Rebrands | MATIC/USDT → POL, FTM/USDT → S | Override table |

Validated against live exchange metadata at runtime. Assets not listed on Hyperliquid (e.g. BCH, CRV, DOT) are caught and rejected with a clear error.

---

## Position Sync (Restart Recovery)

On startup, the bot reconciles local DB state with the actual exchange:

| Scenario | Action |
|----------|--------|
| OPEN in DB, position exists | Verified — no change |
| OPEN in DB, no position | Marked CLOSED (SL/TP filled while offline) |
| PENDING in DB, entry still resting | Verified — no change |
| PENDING in DB, position exists | Promoted to OPEN (filled while offline) |
| PENDING in DB, no order/position | Marked CANCELED (expired while offline) |
| Position on exchange, no DB record | Logged as orphan warning |

Conservative approach — only updates DB state, never auto-opens or auto-closes positions during sync.

---

## Project Structure

```
potion-perps-bot/
├── main.py                           # Entry point — async main loop
├── src/
│   ├── pipeline.py                   # Core orchestrator — classify → parse → size → execute
│   ├── config/
│   │   └── settings.py               # YAML + .env loader, typed dataclasses, validation
│   ├── exchange/
│   │   ├── hyperliquid.py            # HyperliquidClient — wraps SDK Info + Exchange
│   │   ├── order_builder.py          # ParsedSignal → Hyperliquid order params
│   │   └── position_manager.py       # Submit, cancel, close, move SL, position sync
│   ├── input/
│   │   ├── base_adapter.py           # Abstract adapter interface (asyncio.Queue)
│   │   ├── cli_adapter.py            # Paste signals into terminal
│   │   ├── simulation_adapter.py     # Replay .txt files from a directory
│   │   ├── file_adapter.py           # (stub) Watch folder for new files
│   │   └── discord_adapter.py        # (stub) Discord bot listener
│   ├── parser/
│   │   ├── classifier.py             # 10-type MessageType enum + classify()
│   │   ├── signal_parser.py          # TRADING SIGNAL ALERT → ParsedSignal
│   │   └── update_parser.py          # All lifecycle events → typed dataclasses
│   ├── state/
│   │   ├── models.py                 # TradeRecord, OrderRecord, enums
│   │   └── database.py               # SQLite wrapper — trades + orders, user-scoped
│   ├── strategy/
│   │   └── position_sizer.py         # Position sizing + risk gate (check_risk_limits)
│   └── utils/
│       └── symbol_mapper.py          # Potion pair → Hyperliquid coin (110+ mappings)
├── tests/
│   ├── test_classifier.py            # 33 tests — all 28 samples + edge cases
│   ├── test_signal_parser.py         # 8 tests — 4 signals with all fields + errors
│   ├── test_update_parser.py         # 38 tests — all update types + SL parsing + errors
│   ├── test_symbol_mapper.py         # 62 tests — direct, kilo, rebrands, validation
│   └── test_risk_controls.py         # 17 tests — position sizing + risk gate
├── signals/
│   └── samples/                      # 28 real Discord signal samples (all 10 types)
├── config/
│   ├── config.example.yaml           # Full config template with comments
│   └── config.yaml                   # Active config (gitignored)
├── .env.example                      # Credential template
└── requirements.txt
```

---

## Database Schema

Two tables, both scoped by `user_id` for multi-user isolation:

**trades** — one row per signal trade
```
(user_id, trade_id) PRIMARY KEY
pair, coin, side, risk_level, trade_type, size_hint
entry_price, stop_loss, tp1, tp2, tp3
leverage, signal_leverage, position_size_usd, position_size_coin
status (pending → open → closed/canceled)
created_at, updated_at, closed_at, close_reason, pnl_pct
```

**orders** — one row per exchange order
```
id PRIMARY KEY AUTOINCREMENT
trade_id, user_id → FOREIGN KEY to trades
order_type (entry, stop_loss, tp1, tp2, tp3)
coin, side, size, price, oid (Hyperliquid order ID)
status (pending → submitted → filled/canceled/rejected)
fill_price, created_at, updated_at
```

---

## Multi-User Design

Every component is instance-scoped with no global state:

| Component | Isolation |
|-----------|-----------|
| HyperliquidClient | Per-user credentials and client instance |
| Pipeline | Per-user config, client, and database |
| TradeDatabase | All queries filtered by `user_id`; composite PK `(user_id, trade_id)` |
| PositionManager | Scoped via client + database |
| Parsers & Order Builder | Stateless pure functions |
| Input Adapters | Instance-scoped queues |

Multiple users can share the same SQLite database file safely. Each user gets their own `main.py` process with their own config.

---

## Hyperliquid Integration Notes

Lessons learned from testnet testing:

- **szDecimals**: Each asset has a fixed number of size decimal places (e.g. ETH=4, ADA=0, ZK=0). Using the wrong precision causes "invalid size" rejection. Always fetch from `get_asset_meta()`.
- **Price precision**: Hyperliquid uses 5 significant figures for prices. IOC close orders with too many decimals get "invalid price" rejection.
- **Minimum notional**: $10 minimum per order (sz * mid_price, not limit_price).
- **Trigger orders**: `triggerPx` must be a float, not a string. SL/TP orders use `{"trigger": {"triggerPx": float, "isMarket": True, "tpsl": "sl"|"tp"}}`.
- **Portfolio margin**: USDC lives in the spot clearinghouse but is available for perps. Query both spot and perps state for the full balance picture.
- **maxLeverage**: Per-asset from metadata. The bot caps leverage at `min(signal_leverage, config_max, exchange_max)`.

---

## End-to-End Test Results (2026-02-11)

Full pipeline verified on Hyperliquid testnet:

| Step | Test | Result |
|------|------|--------|
| 1 | Connect, check balance ($999.96 USDC) | Pass |
| 2 | Position sync on startup (clean state) | Pass |
| 3 | Process ADA LONG — entry filled, SL + 3 TPs placed | Pass |
| 4 | Verify exchange: 1 position, 4 resting orders | Pass |
| 5 | Duplicate signal rejected | Pass |
| 6 | Process ZK SHORT — second position opened | Pass |
| 7 | 2 positions, 8 orders, $60 exposure tracked | Pass |
| 8 | Cancel trade — 4 orders canceled, position market-closed | Pass |
| 9 | Dynamic SL update — old SL canceled, new SL placed | Pass |
| 10 | Close remaining position — orders canceled, position closed | Pass |
| 11 | Restart sync — clean state, no orphans | Pass |

---

## Implementation Progress

### Phase 1: Foundation — Complete

| Task | Description | Status |
|------|-------------|--------|
| 1.1 | Repo setup, scaffold, requirements | Done |
| 1.2 | Input adapter interface + CLI adapter | Done |
| 1.3 | Simulation adapter (replay signals from files) | Done |
| 1.4 | Collect real signal samples (all message types) | Done — 28 samples |
| 1.5 | Message classifier (10 types) | Done |
| 1.6 | Signal parser (TRADING SIGNAL ALERT → 12 fields) | Done |
| 1.7 | Update parser (TP hit, SL, breakeven, cancel, etc.) | Done |
| 1.8 | Hyperliquid testnet connection + balance check | Done |
| 1.9 | Order builder (uses exchange metadata for szDecimals) | Done |
| 1.10 | Basic execution on testnet (entry + SL + TPs) | Done |
| 1.11 | SQLite state persistence (multi-user isolated) | Done |
| 1.12 | Config system (YAML + .env, typed dataclasses) | Done |
| 1.13 | Unit tests for parsers (68 tests) | Done |

### Phase 2: Strategy Engine & Full Lifecycle — Complete

| Task | Description | Status |
|------|-------------|--------|
| 2.1 | Strategy presets (6 built-in + user-defined) | Done |
| 2.2 | Position sizer (balance-based, risk-level overrides) | Done |
| 2.3 | Pipeline orchestrator (classify → parse → size → build → submit) | Done |
| 2.4 | Dynamic SL adjustment (move_stop_loss + manual update parsing) | Done |
| 2.5 | Trade cancellation (cancel orders + close position) | Done |
| 2.6 | Symbol mapper (110+ pairs, rebrands, kilo-prefix) | Done |
| 2.7 | Position sync on startup (survive restarts) | Done |
| 2.8 | Risk controls (daily loss breaker, exposure cap, 158 tests) | Done |

### Phase 3A: Server-Ready Pipeline — Next

Make the existing pipeline deployable and reliable on a server.

| Task | Description |
|------|-------------|
| 3A.1 | Graceful shutdown & signal handling (SIGTERM, SIGINT) |
| 3A.2 | Retry logic for exchange API calls (transient failures, rate limits) |
| 3A.3 | Health check endpoint (simple HTTP — "am I alive?") |
| 3A.4 | Structured logging (JSON format for server log aggregation) |
| 3A.5 | Docker container + docker-compose for deployment |

### Phase 3B: Multi-User Architecture

Fan-out layer so one signal source serves all users.

| Task | Description |
|------|-------------|
| 3B.1 | Per-user config stored in DB (replace per-user YAML files) |
| 3B.2 | Encrypted credential storage (user API keys at rest) |
| 3B.3 | User registry: add/remove/activate/deactivate users |
| 3B.4 | Multi-user orchestrator: one signal → dispatch to all active user pipelines |
| 3B.5 | Discord adapter: connect to Potion Perps channel, feed signals into the system |

### Phase 4: Telegram Bot

User-facing interface for trade management.

| Task | Description |
|------|-------------|
| 4.1 | Bot setup + user registration flow |
| 4.2 | Credential onboarding ("paste your HL API key") |
| 4.3 | Strategy configuration via Telegram (preset picker, custom params) |
| 4.4 | Signal notifications with approve/reject buttons |
| 4.5 | Three execution modes: manual approval, preset auto, full auto |
| 4.6 | Status commands: /balance, /trades, /pnl, /exposure |
| 4.7 | Admin commands: /kill (emergency stop), /users |

### Phase 5: Polish & Mainnet

| Task | Description |
|------|-------------|
| 5.1 | Mainnet migration with safety checks |
| 5.2 | Rate limiting & abuse prevention |
| 5.3 | Database backup/restore |
| 5.4 | CI/CD pipeline |
| 5.5 | Monitoring dashboard (optional) |

---

## Production Architecture

```
Discord (Potion Perps) → [Signal Ingestion Service] → Signal Queue
                                                          ↓
                                              ┌──── User Pipeline (alice) ──→ Hyperliquid
                                              ├──── User Pipeline (bob)   ──→ Hyperliquid
                                              └──── User Pipeline (...)   ──→ Hyperliquid
                                                          ↕
                                                   Telegram Bot
                                              (notifications, config, approval)
```

- **Signal source**: One process scrapes/receives Potion Perps signals from Discord
- **Multi-user dispatch**: Each signal fans out to every active user's pipeline
- **Per-user pipeline**: Exactly what we built (Phase 1-2), running per-user with their own config and credentials
- **Telegram bot**: User-facing interface for onboarding, strategy config, trade approval, and monitoring
- **Server**: Everything runs on a VPS via Docker, always on

---

## Test Suite

158 tests, all passing (~0.1s):

```
tests/test_classifier.py       — 33 tests (all 28 samples + 5 edge cases)
tests/test_signal_parser.py    — 8 tests (4 signals with all 12 fields + 4 error cases)
tests/test_update_parser.py    — 38 tests (all update types + SL parsing + error cases)
tests/test_symbol_mapper.py    — 62 tests (direct, kilo, rebrands, validation, edge cases)
tests/test_risk_controls.py    — 17 tests (position sizing + risk gate)
```

```bash
python -m pytest tests/ -v
```

---

## Changelog

**2026-02-11 — Phase 2 complete, E2E verified**
- End-to-end testnet run: signal → orders → lifecycle → cleanup all working
- Fixed market close price rounding for low-price assets (5 sig figs)
- Risk controls: daily loss circuit breaker, total exposure cap, consolidated risk gate
- Position sync on startup: reconciles DB state with exchange after restart
- Symbol mapper: 110+ pairs, rebrands (MATIC→POL, FTM→S), kilo-prefix, validation
- Dynamic SL adjustment: parses "move SL to X" from manual updates
- Pipeline orchestrator: full 10-type message handling with auto_execute support
- Position manager: submit, cancel, close, move SL — all exchange operations
- Strategy presets: 6 built-in + user-defined custom presets
- 158 tests total

**2026-02-10 — Phase 1 complete**
- 68 unit tests covering classifier, signal parser, and all update parsers
- Config system: YAML config + .env secrets → typed dataclasses with validation
- SQLite state persistence: trades + orders, composite PK (user_id, trade_id)
- Order builder using real exchange metadata (szDecimals, maxLeverage)
- Full order flow validated on testnet: entry + SL + 3 TPs

**2026-02-09 — Project scaffolded**
- Input adapters (CLI + simulation), parser layer (10 types, 28 samples)
- Hyperliquid testnet connection with portfolio margin support
