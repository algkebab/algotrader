# System Overview

## What the System Is

Algotrader is a paper-trading microservices crypto bot. It trades long positions on Binance Margin Testnet using a pipeline of independently-running Python services that communicate through Redis pub/sub, a shared SQLite database (`algotrader.db`), and a notification queue. No real money is at risk — all orders are simulated with full margin, fee, and interest accounting.

The system runs at 3× leverage against USDT-margined perpetual-style positions (simulated via Binance Margin spot instruments on testnet). Three trading strategies — CONSERVATIVE, AGGRESSIVE, and REVERSAL — run simultaneously, filtered by a real-time market regime detector.

---

## Service Table

| Service | Role | Cycle Time | Primary Inputs | Primary Outputs |
|---|---|---|---|---|
| **Scout** | Fetches OHLCV candles from Binance for the active symbol universe | Continuous, per symbol | Binance REST API | `market_data:{symbol}` Redis keys, `active_symbols` |
| **Filter** | Computes indicators, applies strategy-specific gate criteria, scores and ranks candidates | 10 s loop | `market_data:{symbol}` Redis keys | `filtered_candidates` Redis key, `btc_context`, `market_regime` |
| **Brain** | Calls GPT (or code engine) on filtered candidates; applies BTC macro gate and regime gate | 5 s loop | `filtered_candidates`, `btc_context`, `market_regime` | `signals` Redis list → `trade_commands` |
| **Executor** | Receives trade commands, applies RiskGuard, sizes position via Kelly, writes paper order to DB | Event-driven (blpop) | `trade_commands` Redis list | `orders` table in SQLite, `notifications` |
| **Monitor** | Tracks open positions every 2 s; updates trailing SL; fires partial profit at 1R; closes on TP/SL/time-stop | 2 s loop | Binance REST (live price), `orders` table | `orders` table (close), `balance` table, `notifications` |
| **Risk-Manager** | Checks daily drawdown, portfolio exposure, and minimum balance every 10 s; pauses trading when breached | 10 s loop | `orders` table, `balance` table, settings | `settings.trading_paused`, `notifications` |
| **Backtester** | Walk-forward replay of Filter + decision engine against Binance historical data | On request (blpop) | `backtest_request` Redis key | `backtest_runs` and `backtest_trades` SQLite tables, `notifications` |
| **Messenger** | Telegram bot: surfaces notifications to the user, accepts control commands | Event-driven | `notifications` Redis list, Telegram | `settings` table (commands), `trade_commands` |
| **Dashboard** | Web UI at port 8080 | HTTP request | SQLite, Redis | HTML pages |

---

## Data Flow

```
 Binance REST API
       │
       ▼
 ┌──────────┐    market_data:{symbol}     ┌──────────┐
 │  Scout   │ ─────────────────────────► │  Filter  │
 └──────────┘    active_symbols           └──────────┘
                                               │
                    ┌──────────────────────────┼─────────────────────────┐
                    │ filtered_candidates       │ btc_context             │ market_regime
                    ▼ (Redis, JSON list)        ▼ (Redis, TTL 180s)       ▼ (Redis, TTL 300s)
              ┌──────────┐
              │  Brain   │◄─── OpenAI GPT (or code engine in shared/decision.py)
              └──────────┘
                    │
                    │ signals → trade_commands (Redis lists)
                    ▼
              ┌──────────┐
              │ Executor │ ──► _apply_risk_guard() ──► _place_paper_order()
              └──────────┘             │
                                       │ INSERT INTO orders
                                       ▼
                                ┌─────────────────────────┐
                                │   SQLite: algotrader.db  │
                                │  - orders                │
                                │  - balance               │
                                │  - settings              │
                                │  - signals               │
                                │  - daily_pnl             │
                                │  - backtest_runs         │
                                │  - backtest_trades       │
                                │  - benchmark_prices      │
                                └─────────────────────────┘
                                       │          ▲
                                       │ SELECT   │ UPDATE (close, PnL)
                                       ▼          │
                                 ┌──────────┐     │
                                 │ Monitor  │─────┘
                                 └──────────┘
                                       │ notifications (rpush)
                                       ▼
                                 ┌──────────────┐
                                 │  Messenger   │ ◄──► Telegram
                                 └──────────────┘
                                       │ notifications (rpush)
                                       ▼
                                 ┌──────────────┐
                                 │ Risk-Manager │ ◄── checks balance/drawdown/exposure every 10s
                                 └──────────────┘

Parallel backtesting path:
 Telegram "backtest 90" ──► backtest_request ──► Backtester ──► Binance public API
                                                      │
                                                      ├──► backtest_runs / backtest_trades (SQLite)
                                                      └──► notifications ──► Messenger
```

---

## Redis Key Map

| Key | Written By | Read By | TTL | Format |
|---|---|---|---|---|
| `active_symbols` | Scout | Filter | None (overwritten each cycle) | JSON array of symbol strings: `["BTC/USDT","ETH/USDT",...]` |
| `market_data:{symbol}` | Scout | Filter, Monitor (ATR), Brain (BTC benchmark) | None (overwritten) | JSON: `{last_price, change_24h, volume_24h, high_24h, low_24h, candles_15m, candles_1h, candles_4h}` |
| `btc_context` | Filter | Brain | 180 s | JSON: `{price, change_24h, rsi, vwap_pct, ema_stack_15m, ema_stack_1h, macd_15m, ema_stack_4h, adx_4h, atr_ratio}` |
| `market_regime` | Filter | Brain, Dashboard | 300 s | JSON: `{regime, confidence, btc_4h_alignment, adx_4h, breadth_bullish_pct, breadth_rsi_above_50_pct, vol_regime, atr_ratio, active_strategies, position_size_multiplier, updated_at}` |
| `filtered_candidates` | Filter | Brain | None (getset to `[]` on read) | JSON array; each element includes all `market_data` fields plus: `symbol`, `strategy_name`, `rvol`, `rsi`, `rsi_1h`, `recent_change`, all indicator sub-dicts (`ema_stack_15m`, `ema_stack_1h`, `ema_stack_4h`, `vwap`, `atr`, `bollinger_15m`, `macd_15m`), `filter_score` |
| `signals` | Brain | Brain (internal, rarely used directly) | None (rpush/blpop list) | JSON signal objects (merged candidate + AI analysis + `signal_id`, `position_size_multiplier`, `market_regime`) |
| `trade_commands` | Brain (via Messenger for manual trades) | Executor | None (rpush/blpop list) | JSON: `{symbol, stop_loss_pct, take_profit_pct, strategy_name, signal_id, confidence, position_size_multiplier}` |
| `notifications` | Executor, Monitor, Brain, Risk-Manager | Messenger | None (rpush/blpop list) | JSON: `{type, data}` — types: `trade_confirmed`, `trade_closed`, `trade_failed`, `trade_skipped`, `risk_guard_adjustment`, `risk_manager_alert`, `backtest_complete` |
| `backtest_request` | Messenger (Telegram `backtest N` command) | Backtester | None (rpush/blpop list) | JSON: `{strategy, symbols, days, initial_balance}` |
| `cache:brain_wait:{symbol}` | Brain | Brain | 1200 s (RSI<60), 600 s (RSI 60–65), 300 s (RSI>65) | JSON: `{price: float}` — negative cache suppressing re-analysis of flat setups |
| `cache:brain_price:{symbol}` | Brain | Brain | 1800 s | Float string — last analyzed price; re-analysis suppressed if move < 0.5% |

---

## SQLite Tables Summary

Database path: `data/algotrader.db` (overridden by `DATABASE_PATH` env var). WAL journal mode, NORMAL sync — safe for multi-process concurrent access.

| Table | Purpose | Key Columns |
|---|---|---|
| `orders` | All paper trades — open and closed | `id`, `symbol`, `side`, `amount_usdt`, `entry_price`, `quantity`, `tp_price`, `sl_price`, `status` (open/closed), `opened_at`, `closed_at`, `pnl_usdt`, `pnl_percent`, `close_reason`, `entry_fee_usd`, `exit_fee_usd`, `margin_interest_paid`, `net_pnl_pct`, `borrowed_amount`, `hourly_interest_rate`, `strategy_name`, `session`, `signal_id`, `exit_price`, `hours_held`, `mfe_pct`, `mae_pct`, `balance_at_entry`, `initial_sl_price`, `partial_tp_hit` |
| `balance` | Current virtual USDT balance | `currency` (PK), `amount`, `updated_at` |
| `settings` | System configuration (key-value store) | `key` (PK), `value`, `updated_at` — keys: `trading_paused`, `autopilot`, `max_open_orders`, `max_symbols`, `strategy`, `decision_mode`, `balance_last_day_pnl`, `balance_last_check`, `bot_version`, `timezone_offset_min`, `signal_wait` |
| `signals` | All AI/code verdicts with outcome tracking | `id` (UUID PK), `symbol`, `verdict`, `stats_json`, `prompt`, `response_json`, `outcome` (WIN/LOSS/BREAKEVEN), `outcome_pnl_usdt`, `outcome_pnl_pct`, `outcome_close_reason`, `outcome_closed_at` |
| `daily_pnl` | Per-calendar-day net PnL accumulation | `date` (PK), `pnl_usdt`, `trade_count`, `updated_at` |
| `backtest_runs` | One row per completed backtest run | `id`, `strategy`, `symbol`, `days`, `initial_balance`, `final_balance`, `total_trades`, `win_rate`, `sharpe`, `max_drawdown_pct`, `total_return_pct`, `benchmark_return_pct`, `alpha`, `params_json`, `completed_at` |
| `backtest_trades` | Individual simulated trades from a run | `run_id` (FK), `symbol`, `strategy`, `entry_time`, `exit_time`, `entry_price`, `exit_price`, `sl_price`, `tp_price`, `quantity`, `notional_usdt`, `pnl_usdt`, `pnl_pct`, `close_reason`, `confidence`, `setup_grade`, `sl_pct`, `tp_pct` |
| `benchmark_prices` | BTC price at bot start for alpha calculation | `symbol`, `price`, `recorded_at` |
