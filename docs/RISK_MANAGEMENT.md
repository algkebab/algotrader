# Risk Management Reference

Risk is enforced in layers, from the innermost (per-trade) to the outermost (portfolio-level). Each layer protects against a distinct failure mode.

---

## Layer 1: RiskGuard (Executor)

**Where:** `services/executor/main.py`, `_apply_risk_guard()`

**What it protects against:** Brain producing a stop-loss that is too wide to trade safely, or a risk/reward ratio that is unprofitable over time.

**When it fires:** Every single trade, unconditionally, before position sizing runs.

**What happens:**

```python
# Cap SL at RISK_GUARD_MAX_SL (2.5%)
if sl is None or sl <= 0 or sl > 2.5:
    sl = 2.5   # hard cap

# Enforce minimum R:R = 1.5
if tp is None or tp <= 0 or (tp / sl) < 1.5:
    tp = sl * 1.5   # recompute TP to maintain minimum R:R
```

Both constants come from `shared/config.py`:
- `RISK_GUARD_MAX_SL = 2.5` (percent)
- `RISK_GUARD_MIN_RR = 1.5`

When an adjustment is made, a `risk_guard_adjustment` notification is pushed to Redis and surfaced in Telegram. The trade still proceeds — RiskGuard adjusts rather than rejects, except if the adjusted SL still exceeds the liquidation threshold.

**Liquidation safety check:** If `sl_pct >= 33.0` (the `LIQUIDATION_THRESHOLD_PCT` for 3× leverage), the order is rejected outright with a `trade_failed` notification.

**Note:** This is a safety backstop, not the primary sizing mechanism. Brain and decision.py are expected to produce sensible SL/TP values (ATR-based, strategy-specific) that rarely trigger RiskGuard.

---

## Layer 2: Kelly Position Sizing

**Where:** `shared/portfolio.py`, `kelly_position_size()`

**What it protects against:** Oversizing in weak market conditions, after a drawdown streak, or when the system has low-confidence signals.

**The formula:**

```python
# Step 1: Kelly fraction
b = avg_win_pct / avg_loss_pct           # win/loss ratio
kelly_f = (win_rate * b - (1 - win_rate)) / b
kelly_f = max(0.0, min(kelly_f, 1.0))   # clip to [0, 1]
fractional = kelly_f * 0.25             # 25% of full Kelly (KELLY_FRACTION = 0.25)

# Step 2: Map fractional Kelly to a risk scale
# kelly_f=0 (no edge) → 0.3× base; kelly_f=0.25 (max fractional) → 1.5× base
scale = 0.3 + (fractional / 0.25) * 1.2

# Step 3: Apply multipliers
final = base_risk_pct * scale * confidence_mult * dd_mult * regime_mult

# Step 4: Hard bounds
risk_pct = max(0.002, min(0.025, final))   # never below 0.2% or above 2.5%
```

**Base risk:** `POSITION_RISK_PCT = 0.01` (1% of balance).

**No history case:** When `win_rate` is None (no closed trades yet), the formula outputs `base_risk_pct * 0.5 * confidence_mult * dd_mult * regime_mult`. This starts at 50% of base risk (0.5% of balance) and scales from there.

### Scaling Factors

**Confidence multiplier** (from Brain's confluence signal count):
```python
confidence_mult = 0.5 + (min(confidence, 95) / 95) * 0.75
```

| Confidence | Multiplier |
|---|---|
| 0 | 0.500× |
| 50 | 0.500 + (50/95)×0.75 = 0.895× |
| 70 | 0.500 + (70/95)×0.75 = 1.053× |
| 83 | 0.500 + (83/95)×0.75 = 1.155× |
| 95 | 0.500 + (95/95)×0.75 = 1.250× (max) |

**Drawdown multiplier:**

| Current Drawdown | Multiplier |
|---|---|
| 0–4.99% | 1.0× |
| 5–9.99% | 0.75× |
| 10–14.99% | 0.50× |
| ≥ 15% | 0.25× |

**Regime multiplier** (from `market_regime.position_size_multiplier`):

| Regime | Base Mult | After ELEVATED vol (×0.75) | After EXTREME vol (×0.5) |
|---|---|---|---|
| BULL_TRENDING | 1.0 | 0.75 | 0.50 |
| RANGING | 0.75 | 0.5625 | 0.375 |
| MIXED | 0.75 | 0.5625 | 0.375 |
| BEAR_TRENDING | 0.50 | 0.375 | 0.25 |

**Hard bounds:** `risk_pct` is always clamped to `[0.002, 0.025]` (0.2%–2.5% of balance). No trade can risk below 0.2% (not worth the overhead) or above 2.5% (matches the RiskGuard SL cap, so the maximum dollar risk is bounded).

### Full Worked Example

Inputs: win_rate=0.60, avg_win=2.8%, avg_loss=1.4%, base_risk=1%, confidence=70, drawdown=3%, regime=BULL_TRENDING (mult=1.0)

```
b = 2.8 / 1.4 = 2.0
kelly_f = (0.60 × 2.0 - 0.40) / 2.0 = (1.20 - 0.40) / 2.0 = 0.40
fractional = 0.40 × 0.25 = 0.10
scale = 0.3 + (0.10 / 0.25) × 1.2 = 0.3 + 0.48 = 0.78
confidence_mult = 0.5 + (70/95) × 0.75 = 0.5 + 0.553 = 1.053
dd_mult = 1.0  (drawdown 3% < 5%)
regime_mult = 1.0

risk_pct = 0.01 × 0.78 × 1.053 × 1.0 × 1.0 = 0.00821  (0.821%)
bounded: max(0.002, min(0.025, 0.00821)) = 0.821%
```

On a $1,000 balance: risk = $8.21. With a 1.5% SL: notional = $8.21 / 0.015 = $547 (within 3× leverage = $3,000 max).

---

## Layer 3: Correlation Guard

**Where:** `shared/portfolio.py`, `check_correlation_guard()`. Called by both Filter (to skip candidates) and optionally pre-validated by Brain.

**What it protects against:** Concentrating too many positions in the same market sector, which amplifies correlated losses when that sector sells off.

**Sector map:**

| Sector | Symbols |
|---|---|
| L1 | BTC, ETH, SOL, ADA, AVAX, DOT, ATOM, NEAR, APT, SUI, TON, TRX |
| L2 | MATIC, ARB, OP, STRK |
| DEFI | UNI, AAVE, CRV, LINK, MKR, COMP |
| CEX | BNB, OKB |
| MEME | DOGE, SHIB, PEPE, FLOKI |
| ALT | All other symbols (fallback) |

**Rule:** `MAX_PER_SECTOR = 2`. A third position in the same sector is blocked.

**Why sectors matter:** L1 blockchains (SOL, ETH, ADA) are highly correlated — they tend to fall together in a crypto broad market selloff. If you have 4 L1 positions open, a BTC crash wipes all four simultaneously. Capping at 2 per sector limits this correlated exposure without preventing sector participation entirely.

**Logic:**
```python
count = sum(1 for open_order in open_orders if get_sector(open_order['symbol']) == new_sector)
if count >= 2:
    block ("2/2 positions already in {sector} sector")
```

Symbols not in the sector map default to sector `'ALT'` — they can have up to 2 open positions in this catch-all bucket.

---

## Layer 4: Portfolio Exposure Cap

**Where:** `services/risk-manager/main.py`, `check()` — runs every 10 seconds.

**What it protects against:** Opening so many positions that total notional exposure creates unacceptable risk if markets gap against all of them simultaneously.

**Threshold:** `RISK_PORTFOLIO_EXPOSURE_PCT = 150.0` (overridable via `RISK_PORTFOLIO_EXPOSURE_PCT` env var).

**Check:**
```python
total_notional = sum(order['amount_usdt'] for order in open_orders)
exposure_pct = total_notional / balance * 100
if exposure_pct > 150.0:
    pause_trading("PORTFOLIO EXPOSURE CAP", ...)
```

**Why 150%:** With 3× leverage, 150% notional exposure = 50% of the balance deployed as margin across all positions. This leaves a 50% cash buffer for unrealized losses and margin maintenance. Full 300% would leave no buffer.

**What happens:** `trading_paused` set to `"1"` in the settings table. Filter and Brain check this flag at the top of every loop and sleep until it clears. The flag does not auto-clear — positions must close naturally (TP/SL/time-stop) before the cap is below 150%, at which point the risk-manager stops blocking (but `paused` stays `"1"` until the user sends `start`).

---

## Layer 5: Daily Drawdown Circuit Breaker

**Where:** `services/risk-manager/main.py`, `check()`.

**What it protects against:** A bad trading day cascading into catastrophic loss. Forces a human to consciously decide to resume.

**Threshold:** `RISK_DAILY_DRAWDOWN_PCT = 3.0%` (overridable via `RISK_DAILY_DRAWDOWN_PCT` env var).

**Check:**
```python
today_pnl = get_today_closed_pnl(conn)   # sum of net_pnl_usdt for today's closed orders
if today_pnl < 0:
    drawdown_pct = abs(today_pnl) / balance * 100
    if drawdown_pct >= 3.0:
        pause_trading("DAILY DRAWDOWN LIMIT", ...)
```

**What happens:** Same as exposure cap — `trading_paused = "1"`. The message says "Trading paused until tomorrow — send `start` to override." The user must explicitly send `start` via Telegram to resume. The circuit breaker does NOT auto-reset at midnight. Daily PnL resets at midnight (via the `daily_pnl` table), which means the check will start counting fresh — but `trading_paused` remains `"1"` until a manual `start`.

---

## Layer 6: Balance Guardrail

**Where:** `services/risk-manager/main.py`, `check()` — evaluated before the drawdown check.

**What it protects against:** Attempting to trade with a near-empty account, where fees consume the entire balance.

**Threshold:** `RISK_MIN_BALANCE_USDT = 50.0` (overridable via `RISK_MIN_BALANCE_USDT` env var).

**Check:**
```python
if 0 < balance < 50.0:
    pause_trading("LOW BALANCE", "Balance $X is below the $50 minimum. Add funds then send `start` to resume.")
```

Note the `0 < balance` condition — a zero balance (no DB entry yet or reset) does not trigger this. Only a positive sub-$50 balance triggers it.

---

## Layer 7: BTC Macro Gates (Brain)

**Where:** `services/brain/main.py`, checked at the top of every analysis batch.

**What it protects against:** Taking altcoin long positions when BTC is in a macro bearish state, where all altcoins will fall together regardless of their individual setup quality.

**BTC bias labels** (derived in `_get_btc_bias` from Redis `btc_context`):

| BTC Bias | Conditions | Effect |
|---|---|---|
| `STRONG_BEARISH` | BTC 1h EMA bearish AND BTC 15m EMA bearish AND BTC RSI < 40 | **Blocks entire analysis batch** unless strategy is REVERSAL. No GPT calls made. |
| `BEARISH_HEADWIND` | BTC 1h EMA bearish AND MACD histogram ≤ 0 | **Halves effective max_open** (`ceil(max_open / 2)`). Raises min_confluence +1 for CONSERVATIVE. |
| `BULLISH_TAILWIND` | BTC 1h EMA bullish AND MACD histogram > 0 | No penalty — full capacity. |
| `NEUTRAL` | None of the above | No penalty — full capacity. |

**STRONG_BEARISH block detail:**
```python
if btc_bias == "STRONG_BEARISH" and strategy != "REVERSAL":
    # Skip entire candidates batch — no GPT calls, no positions opened
    time.sleep(5)
    continue
```

This is a hard code-level block, not a prompt suggestion. The AI cannot override it.

**BEARISH_HEADWIND halving:**
```python
if btc_bias == "BEARISH_HEADWIND":
    max_open = math.ceil(max_open / 2)
    # e.g., max_open=4 → 2; max_open=3 → 2; max_open=1 → 1
```

---

## Layer 8: Volatility Overlay

**Where:** `services/filter/main.py` (`_compute_and_store_market_regime`), propagated to Brain via `market_regime.vol_regime`.

**What it protects against:** Taking normal-sized positions when realized volatility is spiking — larger price swings mean stops get hit more frequently, and position sizing must adapt.

**Volatility ratio computation:**
```python
# 14 recent 4h returns vs 50-bar historical 4h returns (RMS comparison)
vol_recent = sqrt(mean(return[i]^2 for i in last 14 bars))
vol_hist   = sqrt(mean(return[i]^2 for i in last 50 bars))
atr_ratio  = vol_recent / vol_hist
```

**Thresholds and effects:**

| atr_ratio | vol_regime | Effect on position_size_multiplier |
|---|---|---|
| ≤ 1.5 | NORMAL | No reduction |
| > 1.5 | ELEVATED | × 0.75 (25% size cut on top of regime) |
| > 2.0 | EXTREME | × 0.50 (50% size cut on top of regime) |

**Combined effect example:** BEAR_TRENDING base mult 0.5 + EXTREME volatility → `0.5 × 0.5 = 0.25` multiplier. Kelly formula receives `regime_multiplier=0.25`.

**Brain also applies the overlay to effective max_open:**
```python
if vol == 'EXTREME':
    max_open = max(1, math.ceil(max_open * 0.5))
elif vol == 'ELEVATED':
    max_open = max(1, math.ceil(max_open * 0.75))
```

---

## Layer 9: Time-Stop (Monitor)

**Where:** `services/monitor/main.py`, checked on every Monitor tick (every 2 seconds).

**What it protects against:** Being trapped in zombie positions that are not hitting SL or TP but are slowly bleeding margin interest and occupying a position slot.

**Logic:**
```python
opened_at = datetime.fromisoformat(trade['opened_at'])
hours_open = (datetime.now(UTC) - opened_at).total_seconds() / 3600
if hours_open >= 24:
    close_position(symbol, current_price, "TIME-STOP")
```

**What happens:** The position is closed at the current market price (Binance last price), with normal exit fee calculation. PnL can be positive, negative, or at breakeven. The time-stop fires before partial profit checks in the Monitor loop.

**Why 24 hours:** With 3× leverage, holding an altcoin for more than a day creates meaningful exposure to macro BTC overnight moves and accumulating interest (`HOURLY_MARGIN_INTEREST_RATE = 0.001%/hour`). The setup that prompted entry is no longer valid after 24 hours — the system assumes any edge has expired.

---

## Layer 10: Portfolio VaR (portfolio.py)

**Where:** `shared/portfolio.py`, `compute_portfolio_var()`. Used by Dashboard and Messenger for reporting.

**What it measures:** Estimated 95% 1-day Value at Risk — the dollar loss that would be exceeded only 5% of trading days under normal market conditions.

**Formula:**
```python
VaR = sum(notional × atr_pct × 1.65  for each open order)
# where atr_pct = atr / price  (ATR as fraction of price)
# 1.65 = 95th percentile z-score for one-tailed normal distribution
# fallback: atr_pct = 0.02 (2%) when ATR unavailable
```

**Interpretation:** If VaR shows $45, there is a 5% chance the portfolio loses more than $45 in a single day under normal conditions. This is a simplified VaR (no correlation between positions, assumes normal returns) — it is a rough indicator, not a rigorous institutional measure. Crypto returns are fat-tailed, so actual extreme losses can exceed VaR.

**Note:** VaR is informational only — it does not trigger any automatic action. The Portfolio Exposure Cap (Layer 4) is the automated guard; VaR is a diagnostic.
