# Scripts

Helper scripts to inject mock data or trigger flows. **Run from project root** so `.env` is loaded:

```bash
# From algotrader/
python scripts/executor/push_mock_trade_command.py
```

## Clear Redis

Flush the entire Redis DB (all keys in the current database):

```bash
python scripts/clear_redis.py
```

Options:

- `--algotrader-only` — remove only algotrader keys (market_data, signals, trade_commands, etc.).
- `-n` / `--dry-run` — only print what would be deleted.

## Executor scripts

Executor **monitors** the Redis list `trade_commands`. These scripts push mock commands in the same format the executor expects. **Start the executor in another terminal** first; when you run a script, the executor will pick up the command and process it.

| Script | Description |
|--------|-------------|
| `executor/push_mock_trade_command.py` | Push one trade command (default: BTC/USDT, 10 USDT). |
| `executor/push_mock_trade_commands.py` | Push multiple symbols (e.g. BTC, ETH, SOL). |

Example:

```bash
# Terminal 1: start executor
cd services/executor && python main.py

# Terminal 2: push mock command (executor will process it)
python scripts/executor/push_mock_trade_command.py
python scripts/executor/push_mock_trade_command.py --symbol ETH/USDT --amount 20
```

## Other service scripts

| Folder | Script | What it writes | Consumed by |
|--------|--------|----------------|-------------|
| scout | `write_mock_market_data.py` | `market_data` | filter |
| filter | `write_mock_filtered_candidates.py` | `filtered_candidates` | brain |
| brain | — | (uses OpenAI; no mock) | — |
| messenger | `write_mock_signals.py` | `signals` | messenger |
| executor | `push_mock_trade_command*.py` | `trade_commands` | executor |
| monitor | `write_mock_active_trades.py` | `active_trades` | monitor |
| risk-manager | `placeholder.py` | — | (stub service) |

**Requirements:** Redis running (`docker compose up redis` or local Redis on localhost:6379). Install script deps from project root: `pip install -r scripts/requirements.txt` (or use the project venv that already has redis and python-dotenv).

**Executor payload format:** Scripts that push to `trade_commands` must send JSON: `{"symbol": "BTC/USDT", "amount": 10}`. The executor reads exactly that and runs `place_smart_order(symbol, amount)`.
