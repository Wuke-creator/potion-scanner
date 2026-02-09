# Potion Perps Bot

Automated trading pipeline that accepts structured trade signals from Potion Perps, parses them into structured data, and executes perpetual futures trades on Hyperliquid.

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp config/config.example.yaml config/config.yaml
```

Edit `.env` with your API credentials and `config/config.yaml` with your preferences.

## Usage

```bash
python main.py
```

## Testing

```bash
pytest tests/
```

## Status

Phase 1 — Foundation. Testnet only.
