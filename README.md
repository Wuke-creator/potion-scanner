# Potion Perps Bot

Automated trading pipeline that accepts structured trade signals from the Potion Perps Discord bot, parses them in real-time, and executes perpetual futures trades on Hyperliquid.

**Status:** Phase 1 — Foundation (testnet only)

---

## Architecture

Four-layer pipeline with pluggable input:

```
Signal Source → Input Adapter → Message Classifier → Parser → Strategy Engine → Order Builder → Hyperliquid API
```

| Layer | Purpose | Status |
|-------|---------|--------|
| **Input Adapters** | Pluggable signal sources (CLI, file watcher, simulation, Discord) | CLI + Simulation done |
| **Parser** | Classify message type, extract all fields into typed dataclasses | Done (10 message types) |
| **Strategy Engine** | Position sizing, TP/SL rules, 7 backtested strategies | Not started |
| **Exchange Executor** | Hyperliquid API wrapper, order building, position management | Connection done |

---

## Project Structure

```
potion-perps-bot/
├── src/
│   ├── input/                      # Layer 1: Signal input adapters
│   │   ├── base_adapter.py         # Abstract interface — adapters push strings onto asyncio.Queue
│   │   ├── cli_adapter.py          # Paste signals into terminal (double-Enter to submit)
│   │   ├── simulation_adapter.py   # Replay .txt files from a directory (timed or burst mode)
│   │   ├── file_adapter.py         # (stub) Watch folder for new signal files
│   │   └── discord_adapter.py      # (stub) Discord bot listener — Phase 4
│   ├── parser/                     # Layer 2: Signal parsing
│   │   ├── classifier.py           # MessageType enum + classify() function
│   │   ├── signal_parser.py        # TRADING SIGNAL ALERT → ParsedSignal dataclass
│   │   └── update_parser.py        # All other message types → typed dataclasses
│   ├── strategy/                   # Layer 3: Strategy engine (not started)
│   ├── exchange/                   # Layer 4: Exchange execution
│   │   ├── hyperliquid.py          # HyperliquidClient — wraps SDK Info + Exchange clients
│   │   ├── order_builder.py        # (stub) Convert parsed signals → order params
│   │   └── position_manager.py     # (stub) Track open positions and lifecycle
│   ├── state/                      # State management (not started)
│   ├── config/                     # Configuration (not started)
│   └── utils/                      # Shared utilities (not started)
├── signals/
│   ├── samples/                    # 27 real Discord signal samples (all message types)
│   └── incoming/                   # Drop zone for file_adapter
├── tests/                          # (stubs)
├── config/
│   ├── config.example.yaml         # Example config (committed)
│   └── config.yaml                 # Active config (gitignored)
├── .env.example                    # Environment variable template
├── main.py                         # Entry point (stub)
└── requirements.txt
```

---

## Signal Format & Supported Message Types

The Potion Perps Discord bot sends various message types. The parser classifies and extracts structured data from each. All messages may contain Discord markdown (`**bold**`, emojis, backticks) which is stripped before parsing.

### 1. SIGNAL_ALERT — New trade signal (→ `ParsedSignal`)
```
TRADING SIGNAL ALERT
PAIR: ZK/USDT #1286
(MEDIUM RISK)
TYPE: SWING
SIZE: 1-4%
SIDE: SHORT
ENTRY: 0.02153
SL: 0.02236          (-56.57%)
TAKE PROFIT TARGETS:
TP1: 0.02113      (26.01%)
TP2: 0.02068      (55.27%)
TP3: 0.01885      (176.22%)
LEVERAGE: 14x
```
Extracted fields: `pair`, `trade_id`, `risk_level`, `trade_type`, `size`, `side`, `entry`, `stop_loss`, `tp1`, `tp2`, `tp3`, `leverage`

Note: Some signals arrive without the "TRADING SIGNAL ALERT" header — the classifier falls back to detecting ENTRY + SL + TP fields.

### 2. TP_HIT — Individual take-profit target hit (→ `TpHit`)
```
✅ TP TARGET 1 HIT
PAIR: SEI/USDT #1256
PROFIT: 16.03%
PERIOD: 23 Minutes
```
Extracted: `pair`, `trade_id`, `tp_number`, `profit_pct`, `period`

### 3. ALL_TP_HIT — All take-profit targets hit (→ `AllTpHit`)
```
🔥ALL TAKE-PROFIT TARGETS HIT
PAIR: BCH/USDT #1284
PROFIT: 282.76%
PERIOD: 9 Hours 39 Minutes
```
Extracted: `pair`, `trade_id`, `profit_pct`, `period`

### 4. BREAKEVEN — Price returned to entry after TP secured (→ `Breakeven`)
```
BREAK EVEN HIT AFTER TP1
PAIR: ZK/USDT #1286
Price has returned to entry after TP1 was secured. Capital protected.
```
Extracted: `pair`, `trade_id`, `tp_secured`

### 5. STOP_HIT — Stop-loss hit (→ `StopHit`)
```
STOP TARGET HIT
PAIR: WIF/USDT #1267
LOSS: -77.7%
```
Extracted: `pair`, `trade_id`, `loss_pct`

### 6. CANCELED — Trade canceled (→ `Canceled`)
Two known formats:
```
PAIR: RENDER/USDT #1265 CANCELED
Trade got posted with significant delay...
```
```
CANCEL DOT/USDT #1249 (price moved too fast)
```
Extracted: `trade_id`, `pair` (optional — may be absent), `reason`

### 7. TRADE_CLOSED — Trade manually closed out (→ `TradeClosed`)
```
TRADE CLOSED OUT
PAIR: INJ/USDT #1253
TRADE CLOSED OUT, AFTER REACHING TAKE PROFIT 2
```
Extracted: `pair`, `trade_id`, `detail`

### 8. PREPARATION — Heads-up, do NOT execute (→ `Preparation`)
```
Trade #1284 Incoming...
PAIR: BCH/USDT
SIDE: SHORT
ENTRY: 515
LEVERAGE 27x
(Prepare, dont place it yet)
```
Extracted: `trade_id`, `pair`, `side`, `entry` (optional), `leverage` (optional)

### 9. MANUAL_UPDATE — Free-form instruction (→ `ManualUpdate`)
```
PAIR ADA/USDT #1259
SET TO LIMIT WHO HAVENT ENTERED
```
Extracted: `trade_id` (optional), `pair` (optional), `instruction`

### 10. NOISE — Filtered out, no parsing
```
@Perp Alert! New post detected!
```

### Trade ID as Lifecycle Key
Every message type extracts a `trade_id` (the `#1286` number). This is the primary key that links all messages in a trade's lifecycle: preparation → signal → TP hits → breakeven/stop/close/cancel.

---

## Hyperliquid Integration

### Account Model
Hyperliquid uses a **master account + API wallet** architecture:
- **Master account** (`HL_ACCOUNT_ADDRESS`) — owns the funds, used for all queries
- **API wallet** (`HL_API_WALLET` / `HL_API_SECRET`) — only signs transactions on behalf of the master account
- Querying with the API wallet address returns empty results (common pitfall)

### Portfolio Margin
The testnet account uses **portfolio margin** (unified trading):
- Spot and perps balances are combined into one account
- USDC sits in the **spot clearinghouse** but is available for perps trading
- `get_balance()` queries both spot and perps state for a complete picture

### Current Capabilities
```python
from src.exchange import HyperliquidClient

client = HyperliquidClient(
    account_address="0x...",  # master account
    private_key="0x...",      # API wallet key
    network="testnet",
)

client.get_balance()          # USDC balance, account value, margin, withdrawable
client.get_account_state()    # Full perps account state
client.get_spot_balances()    # Spot token balances
client.get_open_positions()   # Non-zero positions
client.get_open_orders()      # Pending orders
client.get_all_mids()         # Live market prices
```

---

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp config/config.example.yaml config/config.yaml
```

Edit `.env` with your credentials:
```
HL_ACCOUNT_ADDRESS=0x_your_master_account_address
HL_API_WALLET=0x_your_api_wallet_address
HL_API_SECRET=0x_your_api_wallet_private_key
```

### Quick Verification

Test the parser against all samples:
```bash
python3 -c "
from pathlib import Path
from src.parser.classifier import classify
print('\n'.join(f'{f.name}: {classify(f.read_text().strip()).value}'
      for f in sorted(Path('signals/samples').glob('*.txt'))))
"
```

Test the exchange connection:
```bash
python3 -c "
import os; from dotenv import load_dotenv; load_dotenv()
from src.exchange import HyperliquidClient
c = HyperliquidClient(os.getenv('HL_ACCOUNT_ADDRESS'), os.getenv('HL_API_SECRET'), 'testnet')
print(c.get_balance())
"
```

---

## Implementation Plan

### Phase 1: Foundation (In Progress)
| Task | Description | Status |
|------|-------------|--------|
| 1.1 | Repo setup, scaffold, requirements | Done |
| 1.2 | Input adapter interface + CLI adapter | Done |
| 1.3 | Simulation adapter (replay signals from files) | Done |
| 1.4 | Collect real signal samples (all message types) | Done — 27 samples |
| 1.5 | Message classifier | Done — 10 types |
| 1.6 | Signal parser (TRADING SIGNAL ALERT) | Done |
| 1.7 | Update parser (TP hit, SL, cancellations, etc.) | Done |
| 1.8 | Hyperliquid testnet connection + balance check | Done |
| 1.9 | Order builder | Done — uses exchange metadata for szDecimals + max leverage |
| 1.10 | Basic execution: entry + SL + TP1 on testnet | Done — validated on testnet |
| 1.11 | SQLite state persistence | Done — trades + orders tables, multi-user isolated |
| 1.12 | Config system + .env | Done — YAML + .env, typed dataclasses, validated |
| 1.13 | Unit tests for parser | Next |

### Phase 2: Strategy Engine & Full Lifecycle
Position sizing, all 7 strategies, multi-TP management, SL updates, trade cancellation, symbol mapping, risk controls.

### Phase 3: Robustness & Monitoring
Error handling, auto-reconnect, health checks, structured logging, Docker, CI/CD, kill switch.

### Phase 4: Extensions
Discord bot adapter, Telegram notifications, mainnet migration, web dashboard.

---

## Multi-User Design

The core pipeline is designed for multi-user support from the start:

| Component | Multi-User Ready | Notes |
|-----------|-----------------|-------|
| HyperliquidClient | Yes | Instance-based — each user gets their own client with their own credentials |
| Order Builder | Yes | Pure functions, no shared state |
| Parsers | Yes | Stateless — classify/parse are pure functions |
| Input Adapters | Yes | Instance-scoped queues, no globals |
| Config system | Not yet | Will load per-user credentials + strategy settings (task 1.12) |
| Database | Not yet | Will use `user_id` isolation in all tables (task 1.11) |

All future work (config, state, logging) must maintain per-user isolation. No singletons, no global state, no shared file paths between users.

---

## Strategy Context

7 backtested strategies from January 2026 (97 trades), all using the same signals but differing in entry/TP/SL handling:

| # | Jan P&L | Win % | Approach |
|---|---------|-------|----------|
| 1 | +$3,731 | 72.8% | Full TP runner (let winners run to TP2/TP3) |
| 2 | +$180 | 72.8% | TP1 conservative (close at TP1) |
| 3 | -$185 | 41.9% | Late/tight entry |
| 4 | -$2,324 | 17.6% | TP3-only holds |
| 5 | +$201 | 51.0% | Breakeven stop filter |
| 6 | +$147 | 24.2% | Heavy filtering |
| 7 | +$101 | 72.8% | Conservative size |

Strategy 1 dominates — will be the default. All 7 will be selectable via config.

---

## Changelog

*This README is a living document updated every few pushes.*

**2026-02-10 — Tasks 1.11–1.12 complete**
- SQLite state persistence: trades + orders tables with composite PK (user_id, trade_id)
- Config system: YAML config + .env secrets → typed dataclasses with validation
- All settings configurable: strategy, risk limits, leverage caps, position sizing, database path
- Multi-user isolation verified: two users can share a DB with the same trade IDs

**2026-02-10 — Tasks 1.9–1.10 complete**
- Order builder now uses real Hyperliquid exchange metadata (`szDecimals`, `maxLeverage`) instead of price-based heuristics
- Fixed bug: ETH orders rejected as "invalid size" because heuristic gave 5 decimals instead of the correct 4
- Added notional validation (sz * price >= $10) and exchange max leverage capping
- Validated full order flow on testnet: entry + SL + 3 TPs for ETH
- Added `get_asset_meta()` to HyperliquidClient (cached per session)
- Documented multi-user compatibility — core pipeline is already multi-user safe

**2026-02-09 — Tasks 1.1–1.8 complete**
- Scaffolded project with full directory structure
- Built input adapter interface with CLI and simulation adapters (timed + burst replay)
- Collected 27 real Discord signal samples covering all 10 message types
- Implemented classifier (10 types) and full parser layer (typed dataclasses for each)
- Connected to Hyperliquid testnet — balance verified at $1,000 USDC
- Key learning: Hyperliquid requires master account address for queries, API wallet for signing only; portfolio margin puts USDC in spot clearinghouse
