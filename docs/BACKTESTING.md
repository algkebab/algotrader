# Backtesting Guide

## What It Does

The backtest runner simulates the full algotrader pipeline against one year of historical data from Binance, using the **code decision engine** — a deterministic rule-based system that mirrors the GPT prompt logic.

Key properties:
- **Data isolation**: uses a completely separate `backtest.db`. The live `algotrader.db` is never touched.
- **Look-ahead bias prevention**: all indicator computation uses only candles available at each simulated timestamp (enforced via `bisect` on pre-loaded candle arrays).
- **Portfolio-level simulation**: all symbols are simulated simultaneously at each 15-minute bar, respecting a max of 4 concurrent positions.
- **Version-tracked**: each run records the `BOT_VERSION` from `shared/version.py`, so you can compare results before and after a code change.

---

## What Gets Simulated

### Date Range
Yesterday minus 365 days → yesterday (UTC). Fixed range — no configuration needed.

### Symbols
Top 30 symbols by Binance 24h quote volume at the time the run starts. Stablecoins and leveraged tokens are excluded automatically.

### Position Logic
| Parameter | Value |
|---|---|
| Leverage | 3× |
| Position size | 1% of balance × leverage |
| Taker fee | 0.1% per side |
| Max concurrent positions | 4 |
| Time stop | 24h |
| Trailing stop | ATR × 2.0 |
| Take-profit gate | High of bar ≥ TP price |
| Stop-loss gate | Low of bar ≤ SL price |

### Indicator Warmup
The first 20 days of data are fetched but excluded from simulation. This gives EMA-50 and ADX-14 enough bars to converge before trading decisions start.

---

## Running a Backtest

### From the Dashboard (recommended)
1. Open the dashboard at `http://<VPS_IP>:8080`
2. Navigate to **Backtest** in the left sidebar
3. Select a strategy (CONSERVATIVE / AGGRESSIVE / REVERSAL)
4. Set an initial balance (default: 1000 USDT)
5. Click **Run**
6. The progress bar shows fetch and simulation phases
7. Results appear in the table when done
8. Click any row to drill into individual trades

### From the Command Line (VPS)
```bash
# Inside the dashboard container
docker exec -it algotrader-dashboard-1 python /app/scripts/backtest.py \
  --run-id $(python3 -c "import uuid; print(uuid.uuid4())") \
  --strategy CONSERVATIVE \
  --balance 1000

# Or directly on the host if running outside Docker
cd /path/to/algotrader
python scripts/backtest.py \
  --run-id $(python3 -c "import uuid; print(uuid.uuid4())") \
  --strategy CONSERVATIVE \
  --balance 1000
```

**Note:** When running from the command line, you must first create the run record in `backtest.db`. The dashboard does this automatically. For manual CLI runs, use:

```bash
python3 - <<'EOF'
import sys, uuid
sys.path.insert(0, '.')
from shared import backtest_db
from shared.version import BOT_VERSION
run_id = str(uuid.uuid4())
print(f"Run ID: {run_id}")
with backtest_db.get_connection() as conn:
    backtest_db.init_schema(conn)
    backtest_db.create_run(conn, run_id=run_id, bot_version=BOT_VERSION,
        strategy='CONSERVATIVE', start_date='', end_date='',
        initial_balance=1000.0)
print("Run created. Now run:")
print(f"python scripts/backtest.py --run-id {run_id} --strategy CONSERVATIVE --balance 1000")
EOF
```

---

## Database Location

| Database | Path | Purpose |
|---|---|---|
| Live (untouched) | `/data/algotrader.db` | All live orders, balance, signals |
| Backtest | `/data/backtest.db` | All backtest runs, trades, OHLCV cache |

The backtest DB path can be overridden via the `BACKTEST_DB_PATH` environment variable.

---

## OHLCV Caching

Historical candles are stored in `backtest.db` after the first fetch. Subsequent runs with the same date range skip the Binance API calls and load from cache. This makes re-running the same date range (e.g., to test a different strategy) much faster.

Cache check: the runner checks whether there are >100 bars for the symbol/timeframe within the date range. If yes, it loads from cache.

---

## Interpreting Results

| Metric | What it means |
|---|---|
| Win Rate | % of trades that closed profitable |
| Profit Factor | Gross wins / Gross losses. >1.5 is good, >2.0 is strong |
| Sharpe | Annualized Sharpe ratio approximated from daily P&L. >1.0 is acceptable, >2.0 is good |
| Max Drawdown | Largest peak-to-trough decline in cumulative P&L (USDT, not %) |
| Net P&L | Final balance − initial balance |
| Avg Hold | Average hours per closed position |

### Close Reasons
| Reason | What happened |
|---|---|
| TAKE-PROFIT | Price hit the TP level |
| STOP-LOSS | Price hit the SL level (trail or original) |
| TIME-STOP | Position held for ≥ 24h |
| FORCED-CLOSE | Run ended while position was still open (closed at last bar) |

---

## Known Limitations

1. **Survivorship bias**: symbols are selected by *current* 24h volume, not historical. Coins that delisted or crashed are not included.
2. **No partial fill simulation**: TP/SL fills are assumed to be exact at the price level. In reality, fast moves can cause slippage.
3. **No spread modelling**: Binance taker fee (0.1%) is applied, but bid-ask spread is not.
4. **Bar-close fills**: TP/SL are checked against the bar's high/low. Real fills happen at the exact candle close price in extreme cases.
5. **Market impact**: the model assumes all trades fill at the exact price with no market impact. Large balances would move prices on less-liquid pairs.
6. **No funding rates**: leveraged positions in live trading incur Binance funding rates; these are not simulated.
7. **Indicator divergence**: the code engine uses a simplified version of the GPT logic. Live GPT performance may differ.

---

## Code Decision Engine vs GPT

Backtesting always uses the code engine (`shared/decision.py`). The live Brain service defaults to GPT but can be switched with:

```
set decision code   # via Telegram
set decision gpt    # restore GPT (default)
```

The code engine implements the same confluence rules as the GPT system prompts:
- **CONSERVATIVE**: 3/5 signals required, RSI 45-65, RVOL ≥ 2.0, 1h trend BULLISH/NEUTRAL, min R:R 2.5
- **AGGRESSIVE**: 2/5 signals required, RVOL ≥ 1.5, MACD positive, min R:R 2.0
- **REVERSAL**: RSI < 30, pct_B ≤ 25%, MACD exhaustion, min R:R 3.0

Differences to expect vs live GPT:
- GPT can reason about subtle price structure that the code engine cannot detect
- Code engine is strictly rule-based — no nuance, no context
- Code engine will likely produce more trades (no "GPT skepticism" effect)
- Backtest results should be treated as a directional signal, not an exact prediction

---

## Comparing Runs

Use the Results History table in the dashboard to compare runs across:
- Different strategies with the same balance
- Same strategy before and after a code improvement
- Bot versions (tracked by `bot_version` column)

The `bot_version` field uses the auto-generated name from `shared/version.py` (e.g., `crystal-heron-53`). This updates on every commit, so you can always trace which code version produced which result.
