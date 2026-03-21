# Improvement Ideas

Generated from architecture review on 2026-03-18.
Updated with professional trading review on 2026-03-22.

---

## Critical — Do Not Deploy Real Capital Without These

- [ ] **Backtesting Framework** — Replay historical OHLCV data through Filter + simulated Brain to validate strategy changes in minutes instead of weeks of live paper trading. Without this there is no evidence the core signal has positive expectancy.
- [ ] **Daily Drawdown Circuit Breaker** — Auto-pause trading if daily loss exceeds a threshold (e.g. 3% of capital). Every prop firm and hedge fund has this. Bot currently trades through losing streaks indefinitely.
- [ ] **Portfolio-Level Risk Cap** — `POSITION_RISK_PCT` controls per-trade risk but nothing prevents 10 positions opening simultaneously with 30%+ of capital deployed. Add a hard cap on total open notional exposure.
- [ ] **Correlation Guard** — Before execution, Executor checks how many open positions are in the same correlation bucket (e.g. BTC-correlated altcoins) and skips if too many are open at once. Opening 5 "different" alts during a BTC dump is one leveraged macro bet.

---

## High Impact

- [ ] **Trailing Stop-Loss** — Monitor updates SL in SQLite as price moves favorably, locking in profits. Especially useful for AGGRESSIVE strategy.
- [ ] **Real Risk Manager** — Implement the placeholder `risk-manager` service with: daily drawdown limit (e.g. stop trading if day's loss > 5%), max concurrent exposure per sector, session-based trading hour restrictions.
- [ ] **Signal Feedback Loop** — After a trade closes, update the `signals` table with outcome (win/loss/PnL). Include recent win rate in Brain's GPT-4o prompt so AI self-calibrates over time.
- [ ] **Partial Take-Profit** — Close 50% of position at TP1, let the rest ride to TP2 (e.g. 2× original TP distance). Monitor already has all the machinery.
- [ ] **Market Regime Detection** — Detect trending vs ranging vs high-volatility regime before entering. In a ranging market, fade the edges and use tighter TP. In high volatility, reduce size or stand aside. BTC 30-day ATR relative to its historical average is a simple proxy.
- [ ] **Dynamic Position Sizing** — Fixed `POSITION_RISK_PCT` is suboptimal. Scale size up when recent win rate and conditions are good, down during drawdowns. Kelly Criterion or volatility-adjusted sizing. Brain already returns `confidence` — use it.

---

## Medium Impact

- [ ] **Multi-Timeframe Confirmation** — Check daily trend alignment before any 15m signal fires. A bullish 15m setup against a bearish daily structure is a low-probability trade. Simple daily EMA stack would filter many losing trades.
- [ ] **BTC Dominance as Macro Signal** — BTC.D rising = money flowing into BTC and out of alts. BTC.D falling = altseason conditions. Current BTC bias only tracks price/direction, not dominance — a meaningful additional filter for alt trades.
- [ ] **Funding Rate Check** — If perpetual funding rate is very high (>0.1%), longs are paying a significant premium to hold. Factor into TP targets or skip the trade entirely. Binance exposes this via API.
- [ ] **Analytics → Brain Feedback** — Analytics generates insights about what setups win vs lose but that knowledge never influences future trades. Rolling summary of recent performance (e.g. "reversal setups losing, momentum winning") should be included in Brain's prompt.
- [ ] **Short Selling / Hedging** — In bear markets the bot goes idle because it only longs. Add short capability or hedge open longs with a BTC short when macro turns strongly bearish.
- [ ] **More Technical Indicators in Filter** — Add MACD or Bollinger Band width alongside RSI + RVOL to reduce noise before signals reach Brain, saving OpenAI costs.

---

## Quality of Life

- [ ] **Service Health Alerting** — If Scout produces no data for >10 minutes, send a Telegram alert. Same for Brain silence. Currently a crashed service is invisible until you check /status manually.
- [ ] **Liquidity-Adjusted Position Sizing** — Do not size a position representing >1% of 24h volume. Otherwise real slippage on entry/exit will far exceed the paper model.
- [ ] **Web Dashboard** — Simple read-only FastAPI + HTML dashboard showing balance, open orders, recent signals, PnL curve, win rate by session/strategy/symbol, Sharpe ratio, max drawdown.
- [ ] **News / Sentiment Pre-Filter** — Check crypto news (e.g. CoinGecko API) before Brain calls GPT-4o. Skip a coin if there's a major negative event regardless of technicals.
- [ ] **SQLite → PostgreSQL** — Swap SQLite for PostgreSQL to handle write concurrency as more services become active. `shared/db.py` already abstracts the DB layer, so migration is relatively contained.

---

## Quick Wins

- [ ] **AI Prompt Versioning** — Add `prompt_version` field to `signals` table so you can compare closed trade performance across different Brain prompt iterations.
- [ ] **Balance Guardrail** — Auto-pause trading and send Telegram alert if balance drops below a minimum threshold (e.g. $100). Prevents economically meaningless fractional positions.
- [ ] **Service Health Checks** — Add a simple HTTP `/health` endpoint to each service so Docker can detect stuck services (e.g. Brain container running but no longer calling GPT-4o).
- [ ] **Paper → Live Transition Controls** — Before going live, add a dry-run mode that simulates execution against real order book depth (not just `ticker['last']`) to measure realistic slippage.
