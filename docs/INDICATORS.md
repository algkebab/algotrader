# Technical Indicators Reference

All indicator logic lives in `shared/indicators.py`. The Filter service delegates to these functions; the Backtester uses them directly for historical replay. The functions are stateless — no Redis, no DB. Candle format throughout is `[timestamp_ms, open, high, low, close, volume]`.

---

## 1. RSI (14) — Relative Strength Index

**What it measures:** Momentum — the ratio of average gains to average losses over the last 14 price changes.

**Exact formula (Wilder's smoothing):**
```
deltas = [close[i] - close[i-1]  for i in 1..n]
gains  = [d if d > 0 else 0  for d in deltas]
losses = [-d if d < 0 else 0  for d in deltas]

# Seed with simple average of first 14 values
avg_gain = mean(gains[:14])
avg_loss = mean(losses[:14])

# Wilder's smoothing for all subsequent values
avg_gain = (avg_gain * 13 + gain) / 14
avg_loss = (avg_loss * 13 + loss) / 14

RS  = avg_gain / avg_loss
RSI = 100 - (100 / (1 + RS))
```

Returns `50.0` (neutral) when fewer than 15 candles are available. Returns `100.0` when `avg_loss == 0`.

**Period:** 14 bars on whatever timeframe is provided. Filter and Brain use 15m candles, so RSI(14) reflects the last ~3.5 hours of price action.

**Reading the value:**

| Range | Meaning |
|---|---|
| < 30 | Deeply oversold — only REVERSAL strategy hunts here |
| 30–40 | Oversold zone — Filter allows REVERSAL (rsi_max=30 strict gate) |
| 40–50 | Weak momentum — CONSERVATIVE filter requires ≥40 |
| 45–65 | Optimal for CONSERVATIVE — momentum without overbought |
| 35–85 | Acceptable for AGGRESSIVE |
| > 70 | Excluded by CONSERVATIVE filter (rsi_max=70) |
| > 85 | Hard-blocked for AGGRESSIVE (rsi_max=85 in decision engine) |

**In this bot:** RSI is both a filter gate and a confluence signal. For CONSERVATIVE strategy, RSI 45–65 is required by both the Filter (`rsi_min=40, rsi_max=70` at filter stage, then `45–65` inside `decision.py`) and a hard gate in the code decision engine. The 1h RSI is also checked at the filter stage as `rsi_1h_max` per strategy.

---

## 2. RVOL — Relative Volume

**What it measures:** Whether the current bar is seeing above-average participation relative to the recent 50-bar baseline.

**Exact formula:**
```python
current_vol = candles[-2][5]           # volume of last CLOSED bar
avg_vol = mean(c[5] for c in candles[-(50+2):-2])  # prior 50 bars (not including current)
RVOL = current_vol / avg_vol
```

Returns `0.0` when fewer than 52 candles are available.

**Why `candles[-2]` (not `candles[-1]`):** `candles[-1]` is the currently-forming bar — its volume is incomplete. Using it would produce a misleadingly low reading mid-bar and spike at bar close. `candles[-2]` is the last fully-closed bar, whose volume is final.

**Period:** 50-bar average (not 20-bar as sometimes described in older comments — the code uses `period=50`).

**Reading the value:**

| RVOL | Meaning |
|---|---|
| < 0.7 | Thin volume — price moves are suspect, fakeout risk |
| 0.7–1.2 | Normal — baseline participation |
| 1.2–1.5 | Moderate pickup — minimum for AGGRESSIVE filter |
| 1.5–2.0 | Strong — minimum for CONSERVATIVE filter |
| 2.0–3.0 | High participation — confluence signal fires |
| ≥ 3.0 | Capitulation / breakout spike — required for REVERSAL filter |
| ≥ 4.0 | Capped at 4× in the scoring formula (outliers don't inflate score further) |

**In this bot:** RVOL contributes up to 40 points to the filter score and is a hard gate in the Filter. The scoring formula is `min(rvol / 4.0, 1.0) * 40` — a 4× RVOL scores full marks; a 2× RVOL scores 20/40.

---

## 3. EMA Stack (9 / 21 / 50)

**What it measures:** Short, medium, and long-term trend alignment derived from three exponential moving averages.

**Exact formula (EMA seeded with SMA):**
```python
k = 2.0 / (period + 1)
ema[0] = mean(prices[:period])         # SMA seed
ema[i] = prices[i] * k + ema[i-1] * (1 - k)
```

Multipliers: EMA9: k=0.2, EMA21: k=0.0909, EMA50: k=0.03846.

**Alignment labels (exact conditions from `compute_ema_stack`):**

| Label | Condition | Description |
|---|---|---|
| `BULLISH` | `ema9 > ema21 > ema50` | Full bullish stack — all three EMAs in ascending order |
| `BEARISH` | `ema9 < ema21 < ema50` | Full bearish stack — all three EMAs in descending order |
| `RECOVERING` | `ema9 > ema21` and `ema21 < ema50` | Short-term recovery but still below the long-term trend |
| `WEAKENING` | `ema9 < ema21` and `ema21 > ema50` | Short-term weakening but still above the long-term trend |
| `MIXED` | None of the above | No clear trend (ema21 is between ema9 and ema50 in a crossed configuration) |

**Timeframes computed:** 15m, 1h, and 4h. Each timeframe's EMA stack is computed and stored independently. The 15m stack drives Filter and Brain confluence signals. The 1h stack is a directional gate in CONSERVATIVE (1h must be BULLISH or NEUTRAL — meaning `BULLISH`, `RECOVERING`, or `MIXED`). The 4h stack drives market breadth counting and the regime detection vote.

**In this bot:**
- Filter skips non-REVERSAL symbols with a full BEARISH 15m EMA stack.
- CONSERVATIVE filter rejects symbols with 4h EMA `BEARISH` (WEAKENING is allowed as a pullback setup).
- AGGRESSIVE filter also rejects 4h `BEARISH` but accepts all other alignments.
- EMA stack contributes up to 30 points to the filter score: BULLISH=30, RECOVERING=20, MIXED=10, WEAKENING=5, BEARISH=0.

---

## 4. VWAP — Volume-Weighted Average Price

**What it measures:** The average price weighted by volume since the session opened, giving a "fair value" reference that institutional traders anchor to.

**Exact formula:**
```python
typical_price = (high + low + close) / 3
VWAP = sum(typical_price * volume for each candle) / sum(volume for each candle)
```

Returns `None` when candles are empty or total volume is zero.

**Session reset logic:**
```python
today_midnight_ms = datetime.now(UTC).replace(hour=0, minute=0, second=0).timestamp() * 1000
session_candles = [c for c in candles_15m if c[0] >= today_midnight_ms]
# Fallback: fewer than 10 today → use last 32 candles (~8h)
if len(session_candles) < 10:
    session_candles = candles_15m[-32:]
```

The session resets at **00:00 UTC** each day, matching the Binance UTC daily candle boundary. This is the same reference point institutional algo desks use — VWAP means nothing as a "fair value" reference if computed across multiple sessions.

**Why institutional traders care:** Market makers run VWAP execution algorithms. Prices below VWAP suggest the market has been net selling since the open; prices above suggest net buying. A crypto bot using VWAP is aligning with this institutional flow — entering when price is above VWAP biases toward positions that institutional flow is already supporting.

**In this bot:** VWAP is computed on 15m candles and is one of the five CONSERVATIVE confluence signals (price > VWAP = bullish). It is also a confluence signal in AGGRESSIVE. Decision output includes exact VWAP distance.

---

## 5. ATR (14) — Average True Range

**What it measures:** Typical price range (volatility) over 14 bars, accounting for overnight gaps via the "true range" definition.

**Exact formula (Wilder's smoothing):**
```python
true_range = max(high - low, |high - prev_close|, |low - prev_close|)

# Seed with simple average of first 14 true ranges
atr = mean(true_ranges[:14])

# Wilder's smoothing for subsequent bars
atr = (atr * 13 + tr) / 14
```

Returns `None` when fewer than 15 candles are available (period + 1).

**Period:** 14 bars on 15m candles. This covers approximately 3.5 hours of price history — responsive to intraday volatility shifts without being as noisy as a 5-bar ATR.

**How it's used:**

1. **Stop-loss sizing** (`_compute_sl_pct` in `decision.py`):
   ```python
   atr_pct = atr / price * 100
   sl_pct = max(atr_pct * 1.5, 1.2)   # 1.5× ATR, minimum 1.2%
   ```
   The stop is placed 1.5 ATRs below entry. This adapts to current volatility — a quiet session gets a tighter stop; a volatile session gets a wider stop.

2. **Trailing stop** (Monitor): The trailing stop-loss tracks `current_price - ATR * 2.0` and only moves upward, never down.

3. **Portfolio VaR** (portfolio.py): `VaR contribution = notional × (atr / price) × 1.65`

4. **Volatility ratio** (regime detection): A 14-bar RMS of 4h returns divided by a 50-bar RMS detects ELEVATED (>1.5×) or EXTREME (>2.0×) volatility regimes.

---

## 6. Bollinger Bands (20, 2σ)

**What it measures:** Price location relative to a 20-bar rolling mean ± 2 standard deviations.

**Exact formula:**
```python
window = closes[-20:]          # last 20 closes
middle = mean(window)
variance = mean((x - middle)**2 for x in window)   # population variance
std = sqrt(variance)
upper = middle + 2.0 * std
lower = middle - 2.0 * std

band_range = upper - lower
pct_b = (close[-1] - lower) / band_range * 100    # 0% = at lower, 100% = at upper
bandwidth = band_range / middle * 100              # squeeze detection
```

Returns `None` when fewer than 20 candles available.

**Reading %B:**

| %B | Interpretation |
|---|---|
| ≤ 0% | Price at or below lower band — extreme oversold |
| ≤ 25% | Near lower band — REVERSAL strategy fires here (pct_b ≤ 25 required) |
| 25–75% | Mid-band area — typical consolidation |
| 75–100% | Near upper band — overbought caution |
| ≥ 100% | At or above upper band — momentum breakout (or overextension) |

**REVERSAL hard gate:** `pct_b > 35` blocks REVERSAL trades in both the Filter and decision engine. A reversal entry above 35% of the band is not near enough to the lower band to be a genuine exhaustion setup.

**Bandwidth — squeeze detection:**

```
bandwidth = (upper - lower) / middle * 100
```

A bandwidth below 2% indicates a volatility squeeze — price has compressed into a very tight range. The `_detect_setup_15m` function classifies this as a `CONSOLIDATION` setup pattern.

**In this bot:** Bollinger Bands are not used as a filter gate for CONSERVATIVE or AGGRESSIVE. They are used solely in REVERSAL (via %B) and as a setup classifier in the decision engine.

---

## 7. MACD (12 / 26 / 9)

**What it measures:** The difference between two EMAs (momentum direction and strength), smoothed by a signal line.

**Exact formula:**
```python
ema_fast = compute_ema(closes, 12)    # 12-bar EMA
ema_slow = compute_ema(closes, 26)    # 26-bar EMA
macd_line = ema_fast - ema_slow       # line (series)

signal_line = compute_ema(macd_line, 9)    # 9-bar EMA of MACD line

histogram      = macd_line[-1] - signal_line[-1]
histogram_prev = macd_line[-2] - signal_line[-2]   # previous bar's histogram
```

Returns `None` when fewer than `26 + 9 = 35` closes available. Both EMA computations are seeded with SMA using the same `compute_ema` function.

**Output dict:**
```json
{
  "macd": float,
  "signal_line": float,
  "histogram": float,
  "histogram_prev": float
}
```

**Why `histogram_prev` matters:**

The histogram direction (growing vs. shrinking) is more informative than its absolute value:
- `histogram > 0` and `|histogram| > |histogram_prev|` → momentum accelerating bullish (growing histogram)
- `histogram > 0` and `|histogram| < |histogram_prev|` → momentum positive but fading
- `histogram < 0` and `|histogram| < |histogram_prev|` → sellers losing steam (REVERSAL signal)
- `histogram < 0` and `|histogram| > |histogram_prev|` → selling accelerating

**In this bot:**

- **CONSERVATIVE:** requires histogram positive AND growing (both conditions) for full confluence credit. Positive but not growing adds a weaker signal.
- **AGGRESSIVE:** requires histogram positive. If positive but shrinking for consecutive bars, it becomes a counter-signal.
- **REVERSAL:** requires histogram negative but with SHRINKING magnitude (bears exhausting). A growing negative histogram means sellers still have control — REVERSAL is blocked.
- **Filter score:** 10 pts if `histogram > 0` and `histogram > histogram_prev`; 5 pts if histogram merely positive.

---

## 8. ADX (14) — Average Directional Index

**What it measures:** Trend strength, not direction. A high ADX means a strong trend (could be either up or down). A low ADX means ranging.

**Exact formula (Wilder's smoothing):**
```python
# For each bar:
up   = high - prev_high
down = prev_low - low
+DM  = up   if up > down and up > 0 else 0
-DM  = down if down > up and down > 0 else 0
TR   = max(high - low, |high - prev_close|, |low - prev_close|)

# Smooth over 14 bars (Wilder's method)
TR14  = TR14  - TR14/14  + TR
+DM14 = +DM14 - +DM14/14 + +DM
-DM14 = -DM14 - -DM14/14 + -DM

+DI = 100 * +DM14 / TR14
-DI = 100 * -DM14 / TR14
DX  = 100 * |+DI - -DI| / (+DI + -DI)

# ADX = Wilder-smoothed DX over 14 values
ADX = (ADX_prev * 13 + DX) / 14
```

Requires `period * 2 + 1 = 29` candles minimum. Returns `None` when insufficient data.

**Reading the value:**

| ADX | Trend Strength |
|---|---|
| < 20 | Ranging — no meaningful trend. Regime detection forces RANGING regime regardless of votes. |
| 20–25 | Weak trend forming |
| 25–40 | Trending — ADX vote fires for regime detection |
| > 40 | Strong trend |
| > 60 | Extremely strong trend (rare) |

**ADX does not tell you direction.** A bearish market can have ADX=45 just as well as a bullish market. Direction comes from +DI vs -DI (not separately reported) or from EMA alignment.

**In this bot:** ADX is computed on 4h candles for BTC only (via `btc_context`). It serves two purposes:

1. **Regime gate:** `adx_4h < 20` forces the `RANGING` regime regardless of bull/bear vote counts.
2. **Regime vote:** `adx_4h >= 25` combined with BTC 4h EMA alignment adds 1 bull or bear vote.

ADX is NOT computed per-symbol for the screened universe — it is only computed for BTC as a macro trend-strength indicator.

---

## 9. Filter Score (0–100)

**What it measures:** A composite signal quality score used to rank candidates. Brain processes the highest-scored candidates first, which matters when `max_open_orders` is hit mid-batch.

**Exact formula (from `score_candidate` in `shared/indicators.py`):**

```python
score = 0.0

# RVOL: 40 points (capped at 4× to avoid outliers)
score += min(rvol / 4.0, 1.0) * 40

# EMA alignment: 30 points
ema_pts = {'BULLISH': 30, 'RECOVERING': 20, 'MIXED': 10, 'WEAKENING': 5, 'BEARISH': 0}
score += ema_pts[ema_alignment]

# RSI quality: 20 points (strategy-dependent)
if strategy == 'REVERSAL':
    score += max(0.0, (30 - rsi) / 30 * 20)      # deeper oversold = more points
else:
    score += max(0.0, (1.0 - abs(rsi - 55) / 45) * 20)   # 55 RSI = perfect; decays either side

# MACD histogram: 10 points
if histogram > 0 and histogram > histogram_prev:
    score += 10    # growing positive histogram
elif histogram > 0:
    score += 5     # merely positive
```

**Score breakdown table:**

| Dimension | Weight | Max Points | How to hit max |
|---|---|---|---|
| RVOL | 40 pts | 40 | RVOL ≥ 4.0× |
| EMA alignment | 30 pts | 30 | BULLISH stack (all three EMAs aligned) |
| RSI quality | 20 pts | 20 | RSI = 55 for momentum strategies; RSI ≤ 0 for REVERSAL (theoretical) |
| MACD histogram | 10 pts | 10 | Histogram positive and growing |

**Practical ranges:** Most qualifying candidates score 30–70. A score above 80 requires RVOL ≥ 4×, a full BULLISH EMA stack, RSI near 55, and growing positive MACD — a rare confluence. Candidates that barely pass filter gates typically score 20–40.
