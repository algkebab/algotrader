# Algotrader — Operator's Guide

A practical day-to-day reference for running the algotrader stack.

---

## Table of Contents

1. [First-Time Setup](#1-first-time-setup)
2. [Initial Configuration via Telegram](#2-initial-configuration-via-telegram)
3. [Daily Operation](#3-daily-operation)
4. [Monitoring Performance](#4-monitoring-performance)
5. [Running a Backtest](#5-running-a-backtest)
6. [Tuning the System](#6-tuning-the-system)
7. [Understanding Risk Management](#7-understanding-risk-management)
8. [Troubleshooting](#8-troubleshooting)
9. [Going from Paper to Live](#9-going-from-paper-to-live)

---

## 1. First-Time Setup

### Prerequisites

| Requirement | Notes |
|---|---|
| Docker Engine + Docker Compose v2 | `docker compose version` to verify |
| Telegram bot token | Create via [@BotFather](https://t.me/BotFather); takes 2 minutes |
| Telegram chat ID | Send any message to your bot, then call `https://api.telegram.org/bot<TOKEN>/getUpdates` and find `"chat":{"id":...}` |
| Binance API key + secret | **Only needed for live trading** (executor). Scout uses public API. Paper mode needs only Telegram. |
| OpenAI API key | Only needed when `set decision gpt` (the default). Skip for `code` mode (zero cost). |

### `.env` Setup

Copy the example and fill in values:

```bash
cp .env.example .env
nano .env
```

**Variables reference:**

| Variable | Required | What it does | Where to get it |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Authenticates the messenger bot | [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Yes | Restricts commands to your chat only | `getUpdates` API call (see above) |
| `BINANCE_API_KEY` | For live trading | Used by executor to place real orders | Binance → API Management |
| `BINANCE_SECRET` | For live trading | Paired with the above key | Binance → API Management |
| `OPENAI_API_KEY` | For GPT mode | Brain uses GPT-4o to evaluate signals | [platform.openai.com](https://platform.openai.com) |
| `IS_TESTNET` | For live trading | Set `false` to trade on real Binance | — |
| `DATABASE_PATH` | No | SQLite path override. Default: `/data/algotrader.db` via Docker volume | — |

For paper trading (recommended for new deployments): set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and optionally `OPENAI_API_KEY`. Leave Binance keys empty — executor will simulate fills.

### Starting the Stack

```bash
docker compose up --build -d
```

This builds all service images and starts them in the background. On first run, image builds take 2–5 minutes.

To watch all logs:

```bash
docker compose logs -f
```

To watch a specific service:

```bash
docker compose logs -f messenger
docker compose logs -f brain
```

### Verifying It's Running

1. Check that all containers are up:

   ```bash
   docker compose ps
   ```

   Expected services: `redis`, `scout`, `filter`, `brain`, `risk-manager`, `backtester`, `messenger`, `executor`, `monitor`, `dashboard`.

2. Open your Telegram chat with the bot and send:

   ```
   status
   ```

   Expected reply within a few seconds:

   ```
   📊 Status

   📌 Pipeline: ▶️ running
   🤖 Autopilot: OFF (Buy button on signals)
   🎯 Strategy: CONSERVATIVE
   📈 Symbols: top 25 by volume
   📋 Open orders: 0 / 4 — —
   📢 WAIT signals: OFF
   ⚙️ Model: 3x leverage · 0.1% taker fee · 0.001%/h margin interest

   💵 Balance: 0.00 USDT · ➡️ 0.00 USDT today
   📡 BTC: $... · 🟡 NEUTRAL
   🕐 Scout: ...s ago · 🔍 Candidates: ...
   ```

   If you see `Scout: ⚠️ stale (>6 min)`, wait another minute — scout is still starting up.

---

## 2. Initial Configuration via Telegram

Run these commands in order after first startup. Each command is acknowledged with a confirmation reply.

**Step 1 — Set starting balance**

```
set balance 1000
```

Sets the paper USDT balance to $1,000. Use whatever amount you want to simulate.

**Step 2 — Choose starting strategy**

```
strategy conservative
```

CONSERVATIVE is the recommended starting point: higher win-rate threshold, tighter risk parameters. See section 6 for when to switch.

**Step 3 — Confirm decision engine**

```
set decision gpt
```

GPT mode uses GPT-4o to evaluate each candidate signal. This costs OpenAI API credits (roughly $0.01–0.05 per analyzed signal depending on context). If you want zero AI cost, use `set decision code` instead — it runs a deterministic rule engine that is faster but less nuanced.

**Step 4 — Set max open positions**

```
orders set max 3
```

Starts with 3 simultaneous open positions. This is conservative and appropriate while you evaluate bot behavior. The allowed range is 1–10; default is 4.

**Step 5 — Set symbol universe**

```
set symbols 25
```

Tells Scout to watch the top 25 symbols by 24h volume on Binance. More symbols means more trade candidates but also more noise. 25 is a solid starting point.

**Step 6 — Verify everything**

```
status
```

Confirm strategy, autopilot state, balance, and symbol count all show what you just set.

**Step 7 — Enable autopilot**

```
autopilot on
```

From this point, every BUY signal automatically places a paper order without requiring manual confirmation. The bot also resumes the pipeline if it was paused.

After step 7 the system is fully operational. You should start receiving BUY signal notifications within a few minutes of the first Scout cycle completing.

---

## 3. Daily Operation

### Morning Check

Send these three commands each morning to get a quick health snapshot:

```
status
balance
orders
```

**`status`** tells you:
- Whether the pipeline is running or paused (look for `▶️ running`)
- Autopilot state
- Current BTC market bias (see below)
- How recently Scout ran (stale > 6 min means something is wrong)
- Number of open vs max positions

**`balance`** shows:
- Current USDT wallet
- Today's closed PnL (reset at UTC midnight)
- Change since you last ran `balance` (useful for spotting drift)
- Unrealized PnL for each open position

**`orders`** lists every open position with:
- Entry price and quantity
- Current market price
- Take-profit and stop-loss levels
- Unrealized PnL

### Reading a Signal Notification

When the brain generates a signal you receive:

```
🚀 Signal: ETH/USDT

🤖 Verdict: `BUY` (HIGH)
📝 _RSI oversold bounce with MACD crossover, bullish EMA stack_

📊 Stats
• Price: `$3412.50`
• RSI: `32.1` · RVOL: `2.3x`
• SL/TP: `1.50%` / `3.20%`
• 24h range: `3280.00` – `3510.00`
• Strategy: `CONSERVATIVE`

🔗 TradingView · Binance
```

Field meanings:

| Field | Meaning |
|---|---|
| Verdict | `BUY` or `WAIT`. `WAIT` signals are hidden by default; turn on with `signal wait on`. |
| Confidence | `HIGH` / `MEDIUM` / `LOW` — AI's confidence in the call |
| Reason | Plain-English summary of why Brain fired a signal |
| RSI | Relative strength index. Values below 35 = oversold; used as a buy trigger |
| RVOL | Relative volume vs 20-period average. Values above 1.5x mean unusual activity |
| SL/TP | Stop-loss and take-profit as % from entry price |
| 24h range | Day's high and low, useful for context on where price sits |

When autopilot is OFF, a **Buy** button appears on BUY signals. Click it to manually place the order.

### Reading a Trade Opened Notification

```
✅ Trade opened

📌 #ETHUSDT

💰 Entry: `3412.50`
🎯 Take profit: `3521.80`
🛑 Stop loss: `3361.31`

_Active — orders placed_
```

No action needed — this is a confirmation that the executor opened the position.

### Reading a Trade Closed Notification

```
💰 Trade closed

📌 #ETHUSDT
📋 Take profit

💵 Net PnL: `+8.21` USDT
📈 PnL: `+3.12`%
💰 Gross PnL: `+12.30` USDT
💸 Fees: entry (inc.), exit `0.0034` USDT
🏦 Margin interest: `0.0009` USDT
📈 ROE (on margin): `+9.36`%
⏱️ Time in trade: `4` h
🎯 Strategy: `CONSERVATIVE`

📥 Entry: `3412.50`
📤 Exit: `3521.80`
```

Net PnL is after fees and margin interest. ROE (return on equity) is PnL as a percentage of the margin used (not total notional), so it will be larger than the PnL % due to leverage.

### Pausing and Resuming

| Situation | Command |
|---|---|
| You want to stop new signals (maintenance, bad market) | `stop` |
| You want to resume after a pause | `start` |
| You want manual control over individual orders | `autopilot off` |
| You want full automation again | `autopilot on` |

`stop` halts Filter and Brain — no new candidates are analyzed. Scout, Executor, and Monitor keep running, so existing positions are still managed (TP/SL still trigger).

`autopilot off` keeps the pipeline running and sends signal notifications, but shows a Buy button instead of placing orders automatically. Use this when you want to review signals before committing.

---

## 4. Monitoring Performance

### Stats Commands

```
stats today
stats yesterday
stats week
stats month
stats all
```

Example output for `stats week`:

```
📊 Stats (Last 7 days)

📋 Closed orders: 18
📈 Total PnL: +42.30 USDT
✅ Successful (PnL > 0): 10 (56%)

Closed by:
  🟢 Take profit: 9
  🔴 Stop loss: 8
  ✋ Manual: 1
```

**What to look for:**
- Win rate (successful %) — see targets per strategy below
- Ratio of TP vs SL closes — a healthy bot closes more via TP than SL
- Total PnL trajectory across time periods

### AI Performance Analytics

```
analytics week
```

This triggers a GPT-4o analysis of your recent trade history. It takes 10–30 seconds and returns a narrative report covering: win rate trend, PnL drivers, common loss patterns, and recommended parameter tweaks. Use it weekly to spot systematic issues.

Available periods: `today`, `last` (yesterday), `week`, `month`.

### Portfolio Overview

```
portfolio
```

Shows:
- Total open notional vs balance (exposure %)
- Current drawdown from peak balance
- Sector breakdown of open positions (e.g., DeFi: 2 positions, L1: 1 position)
- Win rate over the last 20 closed trades

High sector concentration (e.g., 3 positions all in DeFi) increases correlated risk. The correlation guard (see section 7) automatically limits this to 2 positions per sector.

### Alpha vs BTC

```
alpha
```

Shows your strategy's total return since inception vs BTC buy-and-hold over the same period. Positive alpha means you are outperforming simply buying and holding BTC.

```
📈 Alpha vs BTC

Strategy return: +8.3%
BTC return: +5.1%
Alpha: +3.2%

_(Starting balance $1000.00 → Current $1083.00)_
```

If alpha is negative for 2+ weeks, reconsider your strategy or market conditions.

### Win Rate Targets by Strategy

| Strategy | Target Win Rate | Action if Below for 2+ Weeks |
|---|---|---|
| CONSERVATIVE | > 50% | Switch to `code` decision mode or reduce `set symbols` |
| AGGRESSIVE | > 45% | Drop back to `strategy conservative` |
| REVERSAL | > 40% | Only use in confirmed trending markets; otherwise switch |

A single bad week is not alarming. Two consecutive weeks below the threshold warrants investigation — run `analytics week` and `backtest 30` to diagnose.

---

## 5. Running a Backtest

### Starting a Backtest

```
backtest 90
```

Queues a walk-forward backtest against the last 90 days of historical data using the current strategy. The backtester service runs it in the background. Results arrive as a Telegram message in 1–3 minutes.

The backtest uses your current strategy setting (whichever was active when you sent the command). Run separate backtests with `strategy conservative` and `strategy aggressive` to compare before switching.

Day range: 7–365 days. Shorter periods (30d) reveal recent market behavior; longer periods (180d) show durability across regimes.

### Reading Backtest Results

```
🔬 Backtest Complete — CONSERVATIVE (90d)

Trades: 47 | Win rate: 54%
Return: +12.4% | BTC: +8.1% | Alpha: +4.3%
Sharpe: 1.34 | Max DD: 8.2%
```

| Metric | Good | Acceptable | Concerning |
|---|---|---|---|
| Win rate | > 55% | 45–55% | < 45% |
| Alpha | > +5% | 0–5% | Negative |
| Sharpe ratio | > 1.5 | 1.0–1.5 | < 1.0 |
| Max drawdown | < 10% | 10–15% | > 15% |

**Sharpe ratio** measures return per unit of risk. Above 1.0 means risk-adjusted returns are positive; above 1.5 is strong.

**Alpha** is return above BTC buy-and-hold. Zero alpha means you're doing no better than just holding BTC — not worth the complexity.

**Max drawdown** is the largest peak-to-trough loss during the test period. Above 15% means the strategy had a stretch where it was losing significantly.

### What to Do With Bad Results

| Problem | Likely Cause | Action |
|---|---|---|
| Win rate < 40%, Sharpe < 0.5 | Strategy mismatched to market | Try `strategy conservative` or `set decision code` then re-backtest |
| Good win rate but negative alpha | Bear market (BTC dropped more) | Normal in bull markets; check if strategy at least preserved capital |
| Max DD > 20% | Too many positions or strategy too aggressive | Reduce `orders set max` and re-backtest |
| Very few trades (< 20 in 90d) | Filters too strict or bearish macro | Check BTC bias in `status`; try `set symbols 50` |

**Always run a backtest before changing strategy settings.** A backtest tells you whether the new configuration would have worked historically before you risk real paper performance.

---

## 6. Tuning the System

### Switching Strategies

```
strategy aggressive
```

The change takes effect on the next Brain analysis cycle (within 1–2 minutes). Available strategies:

| Strategy | Profile | Best For |
|---|---|---|
| `conservative` | Higher entry threshold, tighter SL, smaller positions | Default; sideways or uncertain markets |
| `aggressive` | Lower entry threshold, wider TP targets | Clear trending markets with strong BTC tailwind |
| `reversal` | Looks for oversold bounces and counter-trend moves | High-volatility periods with clear support levels |

Switch back with `strategy conservative` at any time.

### Adjusting Max Open Orders

```
orders set max 5
```

Range: 1–10. Default: 4.

- **Lower (1–2):** Use when testing a new strategy or during volatile markets. Concentrates risk but limits losses.
- **Moderate (3–5):** Good steady-state for most conditions.
- **Higher (6–10):** More diversification but requires higher balance to maintain meaningful position sizes. Only increase after 2+ weeks of solid performance.

### Adjusting the Symbol Universe

```
set symbols 50
```

Range: 5–200. Default: 25.

- **Fewer symbols (10–20):** Less noise, only the most liquid pairs. Better for conservative mode.
- **More symbols (50–100):** More trade candidates, higher signal frequency. Useful in trending markets.
- **Maximum (200):** Not recommended — many low-liquidity pairs get added, increasing false signals.

Start at 25. Increase to 50 after you see the bot performing well and want higher trade frequency.

### Switching Decision Engine

```
set decision code
set decision gpt
```

| Mode | Cost | Speed | Quality |
|---|---|---|---|
| `gpt` | ~$0.01–0.05/signal | 3–10 seconds | High — GPT evaluates context, news sentiment, macro bias |
| `code` | Free | < 1 second | Deterministic — rule-based, no nuance |

Use `code` mode when:
- You want zero OpenAI cost
- You are backtesting and want fast iteration
- You want reproducible, auditable decisions

Use `gpt` mode for live operation where nuanced analysis matters.

### Setting Your Timezone

```
set timezone +2
set timezone -5
set timezone 5.5
```

This only affects timestamps shown in Telegram messages. The system runs in UTC internally. Does not need to match the server timezone.

---

## 7. Understanding Risk Management

The risk-manager service runs continuously and enforces portfolio-level protection rules. You do not control it directly — it acts automatically and notifies you via Telegram when it intervenes.

### Daily Drawdown Limit (3%)

If closed losses today exceed 3% of your current balance, the risk manager pauses the pipeline and sends:

```
🚨 Risk Manager — Daily drawdown limit hit

Today's loss has exceeded 3% of balance.

Send `start` to resume trading.
```

Scout, Executor, and Monitor keep running (existing positions are still managed). Once you have reviewed the situation, send `start` to resume.

The 3% figure is hard-coded in `shared/config.py` (`RISK_DAILY_DRAWDOWN_PCT`).

### Minimum Balance Floor ($50)

If your USDT balance drops below $50, the risk manager pauses the pipeline with a similar alert. Send `start` after reviewing if you want to continue.

### Correlation Guard

Before placing a new order, the system checks how many open positions already belong to the same market sector (e.g., DeFi, L1 blockchains, AI tokens). If a sector already has 2 open positions (`CORR_MAX_PER_SECTOR = 2`), the new order is skipped regardless of signal strength.

You will see a notification like:

```
⏸️ Order skipped

📌 UNI/USDT

Correlation guard: sector already has 2 open positions (DeFi)
```

This is expected behavior — the system is protecting you from over-concentration. It is not an error.

### Kelly Position Sizing

Position sizes are calculated using fractional Kelly (25% of full Kelly criterion). Kelly requires a win rate estimate — when there is no trade history yet, the system assumes 50% win rate, which gives smaller positions. As you accumulate trade history, Kelly sizing adapts to your actual win rate.

Practical effect: your first few trades will be smaller than later ones. This is intentional — the system is being conservative while it learns your win rate.

### RiskGuard Adjustment Notifications

When the Brain produces a signal with a stop-loss that is wider than the maximum allowed (2.5%), the RiskGuard tightens it automatically:

```
🛡️ RiskGuard adjustment

📌 ETH/USDT
🛑 SL: `3.80` → `2.50` (max 2.5%)
🎯 TP: `2.80` → `3.75` (RR≥1.5x)
```

This means the SL was adjusted to the 2.5% cap, and TP was recalculated to maintain at least a 1.5:1 risk/reward ratio. The trade still proceeds — this is a quality control notification, not a failure.

---

## 8. Troubleshooting

### No Signals for Hours

1. Send `status` — check that the pipeline shows `▶️ running` and autopilot is `ON`.
2. Check BTC bias: if it shows `🔴 STRONG BEARISH`, Brain blocks all BUY signals until macro conditions improve.
3. Check Scout freshness: `Scout: ⚠️ stale (>6 min)` means Scout stopped delivering data. Run `docker compose logs -f scout` to diagnose.
4. Check if the bot is paused by risk manager: look for `⏸️ paused` in status output. Send `start` if so.
5. Check candidates count in `status` — if `Candidates: 0`, Filter is rejecting everything. Try `set symbols 50` to widen the universe.

### "Already Have Open Order for This Symbol"

```
⏸️ Order skipped

📌 BTC/USDT

Already have an open order for this symbol
```

This is the deduplication guard working correctly. The system will not open a second position in the same symbol while one is already open. No action needed.

### RiskGuard Adjustment Notification

See section 7. This is normal — it means the AI suggested an overly wide stop-loss and the guard tightened it. The trade is still placed.

### Bot Paused by Risk Manager

Check `balance` to see how much you lost today. If the daily drawdown limit was hit:
1. Review recent closed orders with `stats today` to understand what happened.
2. If conditions look acceptable, send `start` to resume.
3. If markets look bad, consider sending `stop` and waiting until tomorrow.

### Double Telegram Responses

Every command gets two replies — one with the expected response, one with an unexpected/old response. This means two messenger containers are running with the same bot token.

Fix:

```bash
docker compose down
docker compose up -d
```

The `down` command removes all containers. The `up` command starts exactly one set. If you still see duplicates, check for stray containers:

```bash
docker ps -a | grep messenger
```

Remove any extra containers manually with `docker rm <container_id>`.

### Redis State Lost

If you ran `docker compose down -v` (with the `-v` flag), all volumes were wiped. This deletes:
- All Redis keys (system settings, pipeline state)
- The SQLite database (order history, balance, settings)

You will need to redo initial configuration from section 2. **Avoid `docker compose down -v` unless you intentionally want a clean slate.** Use `docker compose down` (without `-v`) to stop containers while preserving data.

### Service Won't Start / Crash Loop

```bash
docker compose logs -f <service-name>
```

Common causes:
- `brain` or `messenger` crash: missing `OPENAI_API_KEY` when in GPT mode. Set it in `.env` or switch to `set decision code`.
- `executor` crash: missing or invalid Binance API keys. Check `BINANCE_API_KEY` and `BINANCE_SECRET`.
- `messenger` crash: missing `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID`.

After fixing `.env`, rebuild and restart:

```bash
docker compose down
docker compose up --build -d
```

### Dashboard Not Accessible

The dashboard runs on port 8080. Default credentials: `admin` / `changeme` (set via `DASHBOARD_USER` and `DASHBOARD_PASSWORD` environment variables in `docker-compose.yml`).

```
http://<your-server-ip>:8080
```

If not accessible, check: `docker compose ps` to confirm dashboard is running, and verify your firewall allows port 8080.

---

## 9. Going from Paper to Live (When Ready)

### Minimum Requirements Before Going Live

Do not switch to live trading until you meet all of these:

| Metric | Minimum Threshold |
|---|---|
| Paper trading duration | 30 days minimum |
| Number of closed paper trades | 50+ trades |
| Paper win rate (conservative strategy) | > 50% |
| Paper backtest Sharpe (90 days) | > 1.0 |
| Paper max drawdown | < 10% |
| Strategy stability | Same strategy working for 3+ consecutive weeks |

Paper trading at $1,000 simulated capital for 30 days is non-negotiable. The system needs enough trades to calibrate Kelly sizing, and you need enough exposure to see how it behaves across different market conditions.

### What to Change in `.env`

1. Add real Binance API credentials:

   ```
   BINANCE_API_KEY=your_real_key
   BINANCE_SECRET=your_real_secret
   ```

2. Set testnet flag to false:

   ```
   IS_TESTNET=false
   ```

3. Rebuild and restart:

   ```bash
   docker compose down
   docker compose up --build -d
   ```

### Position Sizing for Live Start

Start conservatively:

1. Set a real balance lower than what you actually hold — start at 25–50% of your intended trading capital:

   ```
   set balance 500
   ```

   (Even if you have $2,000 in Binance, start the bot's sizing at $500. Increase `set balance` as confidence grows.)

2. Reduce max open orders to 2:

   ```
   orders set max 2
   ```

3. Keep `strategy conservative` until you have 30 real trades with consistent results.

4. Run `alpha` monthly to confirm you are still outperforming BTC buy-and-hold. If alpha turns negative for 60+ days, consider pausing and re-evaluating.

### Why Paper Trade for 30 Days First

- Kelly sizing needs real win-rate history to work well. Early sizing is conservative by design.
- You will encounter edge cases (market crashes, exchange outages, rate limits) that don't appear in backtests.
- Backtest results assume perfect fills. Live fills include real slippage, spread, and partial fills.
- 30 days covers at least one full market cycle including a downturn.

A backtest Sharpe of 1.3 is promising — it does not guarantee live performance. The paper period is your bridge between simulation and real capital.
