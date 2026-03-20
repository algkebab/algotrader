# Improvement Ideas

Generated from architecture review on 2026-03-18.

---

## High Impact

- [ ] **Trailing Stop-Loss** — Monitor updates SL in SQLite as price moves favorably, locking in profits. Especially useful for AGGRESSIVE strategy.
- [ ] **Real Risk Manager** — Implement the placeholder `risk-manager` service with: daily drawdown limit (e.g. stop trading if day's loss > 5%), max concurrent exposure per sector, session-based trading hour restrictions.
- [ ] **Signal Feedback Loop** — After a trade closes, update the `signals` table with outcome (win/loss/PnL). Include recent win rate in Brain's GPT-4o prompt so AI self-calibrates over time.
- [ ] **Partial Take-Profit** — Close 50% of position at TP1, let the rest ride to TP2 (e.g. 2× original TP distance). Monitor already has all the machinery.

---

## Medium Impact

- [ ] **Multi-Timeframe Confirmation** — Scout fetches both 1h and 15m candles. Brain checks short-term trend alignment before entry to reduce false signals.
- [ ] **Correlation Guard** — Before execution, Executor checks how many open positions are in the same correlation bucket (e.g. BTC-correlated altcoins) and skips if too many are open at once.
- [ ] **Dynamic Position Sizing by Confidence** — Brain returns `confidence` (0–100%) but Executor ignores it. Scale risk: `risk_pct = 0.5% + (confidence/100) × 1.0%`. High-confidence signals get larger size.
- [ ] **More Technical Indicators in Filter** — Add MACD or Bollinger Band width alongside RSI + RVOL to reduce noise before signals reach Brain, saving OpenAI costs.

---

## Quality of Life

- [ ] **Web Dashboard** — Simple read-only FastAPI + HTML dashboard showing balance, open orders, recent signals, and PnL charts.
- [ ] **News / Sentiment Pre-Filter** — Check crypto news (e.g. CoinGecko API) before Brain calls GPT-4o. Skip a coin if there's a major negative event regardless of technicals.
- [ ] **Backtesting Mode** — Replay historical OHLCV data through Filter + simulated Brain to validate strategy changes in minutes instead of weeks of live paper trading.
- [ ] **SQLite → PostgreSQL** — Swap SQLite for PostgreSQL to handle write concurrency as more services become active. `shared/db.py` already abstracts the DB layer, so migration is relatively contained.

---

## Quick Wins

- [ ] **AI Prompt Versioning** — Add `prompt_version` field to `signals` table so you can compare closed trade performance across different Brain prompt iterations.
- [ ] **Balance Guardrail** — Auto-pause trading and send Telegram alert if balance drops below a minimum threshold (e.g. $100). Prevents economically meaningless fractional positions.
- [ ] **Service Health Checks** — Add a simple HTTP `/health` endpoint to each service so Docker can detect stuck services (e.g. Brain container running but no longer calling GPT-4o).
