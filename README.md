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
| **messenger**   | Telegram: **stop** / **start**; **autopilot on** / **autopilot off**; **stop wait** / **start wait**; **mute** / **unmute**; **clear redis**; **status**; **orders**; **balance**; **help**. |
| **executor**    | Trade execution on the exchange (place/cancel orders). |

## Quick start

1. Copy the example env file and set your credentials:

   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and set:

   - `BINANCE_API_KEY` / `BINANCE_SECRET` — for executor (and monitor). Scout uses public API only.
   - `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — for messenger

3. Run all services:

   ```bash
   docker compose up --build
   ```

To run in the background:

```bash
docker compose up --build -d
```

### Stop and clear Redis data

To bring everything down **and** remove all Redis data (volume), use `-v`:

```bash
docker compose down -v
```

This stops and removes containers and deletes the `redis_data` volume, so the next `docker compose up` starts with an empty Redis.

To stop without clearing Redis (data persists for next run):

```bash
docker compose down
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

### Orders and balance (SQLite)

Executor and Monitor persist **orders** (trades) and **balance** in a SQLite database so you keep a history of placed orders and current balance. The DB file is shared via the `trading_data` volume (Docker) or `DATABASE_PATH` (default `./data/algotrader.db` locally). Tables: `orders` (open/closed with PnL), `balance` (e.g. USDT). Balance is synced from the exchange when placing an order and updated when a position is closed.

## Local development

Use a **virtual environment** (required on macOS with Homebrew Python — see [PEP 668](https://peps.python.org/pep-0668/)):

```bash
# From project root: create and activate venv once
python3 -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate

# Install dependencies for the service you want to run (example: scout)
pip install -r services/scout/requirements.txt

# Run the service (from root so shared/ is on path, or set PYTHONPATH)
cd services/scout
python main.py
```

Use the shared logger via the parent path or install the project in editable mode so `shared` is on `PYTHONPATH`.

## Development

### Code quality (Ruff)

Lint and format are configured in `pyproject.toml` (Ruff, Python 3.10+). Run locally to match CI:

```bash
# Lint: report issues
ruff check .

# Format: fix style
ruff format .
```

Fix auto-fixable lint issues:

```bash
ruff check . --fix
```

### Pre-commit hooks

Install [pre-commit](https://pre-commit.com/) and run Ruff + YAML checks before each commit:

```bash
pip install pre-commit
pre-commit install
```

After that, `git commit` will run `check-yaml` and `ruff` (with `--fix`) automatically.

### CI/CD

GitHub Actions (`.github/workflows/ci.yml`) runs on every push and pull request to `main`:

- Ruff check and format check
- Pytest (placeholder until tests are added)

Keep the build green by running `ruff check .` and `ruff format .` (or pre-commit) before pushing.
