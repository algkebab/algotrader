# Market Conditions Reference

The regime detection system classifies the current market into one of four states every 10 seconds (Filter cycle). The regime drives strategy gating, position sizing, and Brain's effective capacity.

---

## Regime Detection Logic

**Where:** `services/filter/main.py`, `_compute_and_store_market_regime()`

**Inputs:**
- `btc_context` (Redis): BTC 4h EMA alignment, BTC ADX (4h), BTC RSI (15m), BTC atr_ratio
- `breadth_stats` (computed each scan cycle from the active symbol universe):
  - `breadth_bullish_pct`: % of symbols with bullish or recovering 4h EMA
  - `breadth_rsi_pct`: % of symbols with RSI > 50

**Vote counting (5 possible bull votes, 5 possible bear votes):**

| Signal | Bull vote | Bear vote |
|---|---|---|
| BTC 4h EMA alignment | `BULLISH` or `RECOVERING` | `BEARISH` or `WEAKENING` |
| Market breadth | ≥ 55% bullish 4h EMA | ≤ 40% bullish 4h EMA |
| ADX × direction | ADX ≥ 25 AND BTC 4h EMA bull | ADX ≥ 25 AND BTC 4h EMA bear |
| BTC RSI (15m) | RSI ≥ 50 | RSI < 50 |
| Breadth RSI | ≥ 55% of symbols RSI > 50 | < 45% of symbols RSI > 50 |

**Classification:**

```python
if adx_4h < 20 and adx_4h > 0:
    regime = "RANGING"
    confidence = max(30, int((1 - adx_4h / 20) * 80))
elif bull_votes >= 3:
    regime = "BULL_TRENDING"
    confidence = int(bull_votes / 5 * 100)
elif bear_votes >= 3:
    regime = "BEAR_TRENDING"
    confidence = int(bear_votes / 5 * 100)
else:
    regime = "MIXED"
    confidence = 40
```

**ADX rule takes priority:** If BTC ADX(4h) is below 20, RANGING is forced regardless of bull/bear vote counts. A strong set of bull votes with ADX=12 still produces RANGING.

**Redis publish:** `market_regime` key with 300-second TTL (5 minutes). Brain and Dashboard read this; the Filter republishes it at the end of every 10-second scan cycle.

---

## BULL_TRENDING

### Detection — Example Numbers

- BTC 4h EMA: BULLISH (ema9 > ema21 > ema50)
- ADX (4h): 31.5 (≥ 25)
- BTC RSI: 58 (≥ 50)
- Market breadth: 67% bullish 4h EMA (≥ 55%)
- Breadth RSI > 50: 61% (≥ 55%)

Votes: BTC EMA bull (+1) + breadth ≥ 55% (+1) + ADX×bull (+1) + RSI ≥ 50 (+1) + breadth RSI (+1) = **5 bull votes**. Confidence = 100%.

### Typical Market Description

BTC is trending upward on the 4h. Most altcoins are following. Volume is above average. Breakout setups appear frequently, and momentum strategies work well. The market rewards buying strength.

### Active Strategies

`[CONSERVATIVE, AGGRESSIVE, REVERSAL]` — all three run.

### Position Size Multiplier

`1.0` (full sizing). Volatility overlay may reduce: ELEVATED → 0.75, EXTREME → 0.50.

### What Changes in Brain

- BTC bias likely `BULLISH_TAILWIND` (EMA 1h bullish, MACD positive)
- No max_open reduction from BTC gates
- No confluence bar increase
- All three strategies compete for position slots

### What the Filter's Candidates Typically Look Like

High RVOL breakouts and continuation plays dominate. EMA stacks are mostly BULLISH. RSI is in the 50–65 range. AGGRESSIVE candidates appear (4h change often ≥ 2%). REVERSAL candidates are rare — few symbols are oversold when the broad market is trending up.

### What GPT/decision.py Is Likely to Say

High BUY signal rate. CONSERVATIVE setups with 4–5 confluences are common. AGGRESSIVE frequently triggers with strong RVOL and EMA alignment. Decision engine confidence is high (70–85 range).

### Typical Trading Day

- 5–10 candidates pass Filter per cycle
- Brain opens 2–4 new positions during the day
- Most positions hit TP within 4–12 hours
- Win rate tends toward the higher end of historical distribution
- Position sizes are at full Kelly-adjusted level

---

## BEAR_TRENDING

### Detection — Example Numbers

- BTC 4h EMA: BEARISH (ema9 < ema21 < ema50)
- ADX (4h): 38.0 (≥ 25)
- BTC RSI: 34 (< 50)
- Market breadth: 18% bullish 4h EMA (≤ 40%)
- Breadth RSI > 50: 22% (< 45%)

Votes: BTC EMA bear (+1) + breadth ≤ 40% (+1) + ADX×bear (+1) + RSI < 50 (+1) + breadth RSI < 45% (+1) = **5 bear votes**. Confidence = 100%.

### Typical Market Description

BTC is in a confirmed downtrend on the 4h. Altcoins are selling off broadly. Volume spikes are red candles. Momentum strategies produce false signals — breakouts reverse. Only counter-trend setups (reversal from exhaustion) have a structural edge.

### Active Strategies

`[CONSERVATIVE, REVERSAL]` — AGGRESSIVE is excluded.

**Note:** CONSERVATIVE is included to capture "relative strength" symbols — coins that are holding up while everything else falls, which often become the next leaders when the market reverses. However, in practice, CONSERVATIVE triggers rarely in BEAR_TRENDING because the 4h EMA gate blocks most symbols (most have BEARISH 4h EMA), and BTC `STRONG_BEARISH` bias (common in BEAR_TRENDING) may block the entire batch.

### Position Size Multiplier

`0.5` (half sizing). With EXTREME volatility: 0.25.

### What Changes in Brain

- BTC bias is often `STRONG_BEARISH` (EMA 1h bearish, EMA 15m bearish, RSI < 40), which blocks CONSERVATIVE entirely and restricts to REVERSAL-only
- Even if only `BEARISH_HEADWIND`, max_open is halved (e.g., 4 → 2)
- CONSERVATIVE confluence bar raised +1 under BEARISH_HEADWIND
- Effective max_open further reduced by EXTREME volatility overlay

### What the Filter's Candidates Typically Look Like

Almost nothing passes for CONSERVATIVE — most symbols have BEARISH 4h EMA and are excluded. REVERSAL candidates appear: deeply oversold symbols (RSI ≤ 30, RSI 1h ≤ 40, 4h drop ≥ 2.5%, RVOL ≥ 3.0×). These are genuine panic-selling setups.

### What GPT/decision.py Is Likely to Say

CONSERVATIVE: WAIT (blocked by BTC gate or insufficient confluence). REVERSAL: BUY on genuine exhaustion (if RSI < 25, %B ≤ 15%, capitulation RVOL ≥ 4×, sellers exhausting). Decision engine emphasizes the "bears exhausting" MACD signal.

### Typical Trading Day

- 0–2 positions opened (all REVERSAL)
- Sizes are at 50% of normal (plus any volatility overlay)
- Time-stops fire more frequently — bear market bounces often fade
- Win rate on REVERSAL in BEAR_TRENDING depends on catching the actual exhaustion point; false reversal entries (MACD still accelerating) produce quick SL hits

---

## RANGING

### Detection — Example Numbers

- BTC 4h EMA: RECOVERING or MIXED
- ADX (4h): 14.0 (< 20 → RANGING forced)
- BTC RSI: 51
- Market breadth: 48% bullish (neither ≥ 55% nor ≤ 40%)

ADX 14.0 < 20 → RANGING regardless. Confidence = max(30, int((1 - 14/20) × 80)) = max(30, 24) = 30%.

Note: ADX=0 (unavailable) skips the RANGING check — only `adx_4h < 20 and adx_4h > 0` forces RANGING.

### Typical Market Description

No trend. BTC is coiling in a tight range. Volume is below average. Breakout attempts fail — price returns to the range mid-point. Mean-reversion setups work; trend-following does not.

### Active Strategies

`[CONSERVATIVE, REVERSAL]` — AGGRESSIVE excluded because breakout setups fail in ranges.

### Position Size Multiplier

`0.75` (75% sizing). With ELEVATED volatility: 0.5625. With EXTREME: 0.375.

### What Changes in Brain

- BTC bias is likely `NEUTRAL` (no clear direction)
- No max_open reduction from BTC gates (unless bias shifts)
- CONSERVATIVE requires grade A — with weaker momentum (lower RVOL, MIXED EMA stacks), A-grade is harder to achieve
- REVERSAL can fire when a symbol hits the lower range boundary with RSI < 30

### What the Filter's Candidates Typically Look Like

EMA stacks are MIXED or RECOVERING. RVOL is modestly elevated on range boundary tests. RSI oscillates near 40–60. CONSERVATIVE candidates often have 2–3 signals (not the 4 needed for grade A). REVERSAL candidates appear when the range low is tested.

### What GPT/decision.py Is Likely to Say

CONSERVATIVE: WAIT ("insufficient confluence" — EMA MIXED, MACD flat). REVERSAL: occasional BUY on range low tests. Decision engine notes "price at lower BB" and "RSI oversold" but may still WAIT if RVOL below 3.0×.

### Typical Trading Day

- 0–2 new positions
- REVERSAL trades from range-low bounces have tighter TP targets (the range ceiling, not a trending target)
- Conservative trades that do fire are higher-quality because the A-grade bar filters out most noise
- Exit via TP at range top or time-stop after 24h if price drifts mid-range

---

## MIXED

### Detection — Example Numbers

- BTC 4h EMA: WEAKENING (ema9 < ema21, ema21 > ema50)
- ADX (4h): 22.5 (above 20 — no RANGING force)
- BTC RSI: 47 (< 50)
- Market breadth: 44% bullish (between 40–55% — neither vote fires)
- Breadth RSI > 50: 46% (between 45–55% — neither vote fires)

Votes: BTC EMA WEAKENING → bear vote (+1). BTC RSI 47 < 50 → bear vote (+1). No other votes. Total: 0 bull, 2 bear. Regime = **MIXED** (< 3 bear votes). Confidence = 40%.

### Typical Market Description

No consensus. BTC is showing early weakness (WEAKENING EMA stack) but ADX is too low to confirm a bear trend. Some sectors are holding up; others are falling. The market is in transition — either a trend is about to start (either direction) or consolidation will continue.

### Active Strategies

`[CONSERVATIVE, REVERSAL]` — same as RANGING. AGGRESSIVE excluded.

### Position Size Multiplier

`0.75`. Same as RANGING.

### What Changes in Brain

- BTC bias is likely `BEARISH_HEADWIND` (EMA 1h weakening/bearish, MACD ≤ 0): max_open halved, confluence +1 for CONSERVATIVE
- With max_open halved, fewer new positions can be added
- REVERSAL can fire if enough symbols have sold off individually even while the market is "mixed"

### What the Filter's Candidates Typically Look Like

Fewer candidates than BULL_TRENDING. Some symbols still have bullish 4h EMAs (relative strength). Those are CONSERVATIVE candidates, but the BEARISH_HEADWIND BTC gate makes the bar high. REVERSAL candidates from weaker sectors that have already broken down.

### What GPT/decision.py Is Likely to Say

CONSERVATIVE: high WAIT rate due to BEARISH_HEADWIND raising confluence requirement. Only the cleanest setups (all 5 signals) produce a BUY. Reason often: "BEARISH_HEADWIND BTC reduces effective capacity." REVERSAL: BUY on individual symbol capitulations, sized at 75% regime.

### Typical Trading Day

- 0–2 new positions
- Bot is cautious — BEARISH_HEADWIND halves the open position slots
- Positions that do open tend to be high-quality (BUY requires A-grade under headwind conditions)
- The bot may be completely flat (no open positions) for extended periods, waiting for a regime shift or a clean REVERSAL setup

---

## Regime Strategy Summary Table

| Regime | Active Strategies | Size Mult | BTC Bias Impact | Typical Daily Positions |
|---|---|---|---|---|
| BULL_TRENDING | CON, AGG, REV | 1.0× | TAILWIND likely — no penalty | 2–4 |
| BEAR_TRENDING | CON, REV | 0.5× | STRONG_BEARISH common — blocks CON | 0–2 (REV only) |
| RANGING | CON, REV | 0.75× | NEUTRAL likely — no reduction | 0–2 |
| MIXED | CON, REV | 0.75× | HEADWIND common — halves max_open | 0–2 |
