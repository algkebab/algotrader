# Algotrader

Microservices-based algorithmic trading system.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- (Optional) Python 3.11+ for local development

## Services

| Service         | Description |
|-----------------|-------------|
| **scout**       | Data collection from exchanges (order books, tickers). |
| **filter**      | Liquidity and trash filtering; keeps only tradeable instruments. |
| **brain**       | Strategy execution and GPT-backed analysis and signals. |
| **risk-manager**| Capital protection, position sizing, and risk limits. |
| **messenger**   | Telegram bot interface for alerts and commands. |
| **executor**    | Trade execution on the exchange (place/cancel orders). |

## Quick start

1. Copy the example env file and set your credentials:

   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and set:

   - `BINANCE_API_KEY` / `BINANCE_SECRET` — for scout and executor
   - `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — for messenger

3. Run all services:

   ```bash
   docker compose up --build
   ```

To run in the background:

```bash
docker compose up --build -d
```

## Project layout

```
algotrader/
├── docker-compose.yml    # Orchestrates all 6 services
├── .env.example          # Env var templates (copy to .env)
├── shared/               # Shared code (logger, etc.)
│   ├── __init__.py
│   └── logger.py
└── services/
    ├── scout/            # Data collection (ccxt)
    ├── filter/           # Liquidity/filtering
    ├── brain/            # Strategy & GPT analysis
    ├── risk-manager/     # Risk & position sizing
    ├── messenger/        # Telegram bot
    └── executor/         # Exchange execution
```

Each service has its own `main.py`, `requirements.txt`, and `Dockerfile`.

## Local development

From the project root, run a single service:

```bash
cd services/scout
pip install -r requirements.txt
python main.py
```

Use the shared logger via the parent path or install the project in editable mode so `shared` is on `PYTHONPATH`.
