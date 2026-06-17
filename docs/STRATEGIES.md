# Trading Strategies Reference

The bot runs three strategies simultaneously. Each strategy is a complete set of filter criteria (applied by the Filter service), confluence rules (applied by Brain/decision.py), and position sizing behavior. The active strategy set is narrowed by the market regime at each scan cycle.

---

## Strategy Profiles at a Glance

| Parameter | CONSERVATIVE | AGGRESSIVE | REVERSAL |
|---|---|---|---|
| **Setup type** | Momentum pullback into VWAP with trend alignment | Breakout with fresh momentum | Exhaustion bounce from oversold extreme |
| **min_24h_volume** | $10,000,000 | $5,000,000 | $20,000,000 |
| **rvol_threshold** (Filter) | 1.5× | 1.2× | 3.0× |
| **rsi_min** (Filter) | 40 | 35 | 0 |
| **rsi_max** (Filter) | 70 | 85 | 30 |
| **rsi_1h_max** (Filter) | 70 | 80 | 40 |
| **min_change (4h)** | +0.3% | +2.0% | −2.5% |
| **4h EMA gate** | Rejects pure BEARISH | Rejects pure BEARISH | Exempt (counter-trend) |
| **min_confluence** (decision.py) | 3 of 5 | 2 of 5 | 2 of 3 (exhaustion signals) |
| **min_rr** | 2.5 | 2.0 | 3.0 |
| **RSI hard gate** (decision.py) | 45–65 required | >85 hard cap | <30 required |
| **sl_default_pct** | 1.5% | 1.5% | 2.5% |
| **Grade required** | A (≥ 4 signals) | B (≥ 2 signals) | A (≥ 3 signals) |
| **Active regimes** | All | BULL_TRENDING only | All |

---

## CONSERVATIVE Strategy

### What It Hunts

Momentum setups where price is already in an uptrend across all timeframes, pulls back to VWAP, and shows renewed participation (RVOL ≥ 2.0×) with RSI in the 45–65 sweet spot — bought but not overbought.

### Filter Criteria Checklist (exact values)

- [ ] 24h volume ≥ $10,000,000
- [ ] RVOL ≥ 1.5× (last closed 15m bar vs 50-bar average)
- [ ] RSI (15m) between 40 and 70
- [ ] RSI (1h) ≤ 70 (not overbought on the higher timeframe)
- [ ] 4h price change ≥ +0.3% (some momentum in the last 4 hours)
- [ ] 15m EMA stack NOT `BEARISH` (pre-filter)
- [ ] 4h EMA alignment NOT `BEARISH` (WEAKENING is allowed)
- [ ] Symbol not already in an open position
- [ ] Sector correlation guard passes (max 2 per sector)

### Brain / decision.py Criteria

**Hard gates (any one blocks immediately):**
- 1h trend (derived from 1h EMA stack) == `BEARISH` → blocked
- RSI outside 45–65 → blocked

**5 Confluence Signals (need ≥ 3 for grade B, ≥ 4 for grade A):**
1. Price > VWAP
2. 15m EMA stack in `{BULLISH, RECOVERING}`
3. MACD histogram positive AND growing (`|histogram| > |histogram_prev|`)
4. RSI between 45 and 65
5. RVOL ≥ 2.0×

CONSERVATIVE requires grade A (≥ 4 signals) to produce a BUY verdict.

**Under BEARISH_HEADWIND BTC bias:** min_confluence raised to 4 (effectively requiring A+ — all 5 signals, though grade caps at A).

**R:R minimum:** 2.5:1 — if `tp_pct / sl_pct < 2.5`, verdict is WAIT.

### Active Regimes

All four regimes (`BULL_TRENDING`, `BEAR_TRENDING`, `RANGING`, `MIXED`) allow CONSERVATIVE. However:
- `BEAR_TRENDING`: position_size_multiplier = 0.5 (half sizing)
- `RANGING` / `MIXED`: position_size_multiplier = 0.75

### Position Sizing Behavior

Kelly formula is applied with the regime multiplier incorporated. At baseline (60% win rate, 70 confidence, 0% drawdown, BULL regime), risk is approximately 1.0–1.3% of balance. See `RISK_MANAGEMENT.md` for full Kelly calculation example.

---

## AGGRESSIVE Strategy

### What It Hunts

Breakout entries where price is already moving with momentum: fresh MACD crossover, expanding volume, 15m EMA stack aligned bullish. Higher RSI tolerance (up to 85) accepts entries earlier in a breakout before RSI resets. Lower volume requirement ($5M) expands the symbol universe.

### Filter Criteria Checklist (exact values)

- [ ] 24h volume ≥ $5,000,000
- [ ] RVOL ≥ 1.2× (lower bar — breakouts often start before volume fully confirms)
- [ ] RSI (15m) between 35 and 85
- [ ] RSI (1h) ≤ 80
- [ ] 4h price change ≥ +2.0% (strong recent directional move required)
- [ ] 15m EMA stack NOT `BEARISH`
- [ ] 4h EMA alignment NOT `BEARISH`
- [ ] Symbol not already in an open position
- [ ] Sector correlation guard passes

### Brain / decision.py Criteria

**Hard gates (any one blocks):**
- 15m EMA stack NOT in `{BULLISH, RECOVERING}` → blocked (required for AGGRESSIVE)
- RSI > 85 → overbought hard cap, blocked

**5 Confluence Signals (need ≥ 2 for grade B — sufficient for BUY):**
1. 15m EMA stack in `{BULLISH, RECOVERING}`
2. RVOL ≥ 1.5×
3. MACD histogram positive (if shrinking, becomes a counter-signal)
4. 1h EMA stack in `{BULLISH, RECOVERING}` (NEUTRAL is acceptable; BEARISH requires RVOL ≥ 3.0× to override)
5. Price > VWAP

AGGRESSIVE accepts grade B (≥ 2 signals). Grade A (≥ 3) is not required.

**Under BEARISH_HEADWIND BTC bias:** min_confluence raised to 3 (requiring B+ effectively, since only 2 signals would pass under normal conditions).

**R:R minimum:** 2.0:1.

### Active Regimes

AGGRESSIVE is only active in `BULL_TRENDING` regime. In BEAR, RANGING, and MIXED regimes, AGGRESSIVE is excluded from `active_strategies` and any AGGRESSIVE candidate is blocked at both Filter and Brain levels.

### Position Sizing Behavior

BULL_TRENDING regime means `position_size_multiplier = 1.0`. Kelly sizing at full regime scale. AGGRESSIVE is never active in reduced-sizing regimes, which means it only runs when the system is at full capacity.

---

## REVERSAL Strategy

### What It Hunts

Counter-trend entries after a genuine capitulation: RSI below 30 (deeply oversold on 15m), price at the lower Bollinger Band (%B ≤ 25%), RVOL ≥ 3.0× (panic selling volume spike), and evidence that sellers are exhausting (MACD bearish but histogram shrinking). Requires the 1h to also confirm oversold (rsi_1h ≤ 40) — prevents entering on a 15m blip when the 1h trend is merely consolidating.

### Filter Criteria Checklist (exact values)

- [ ] 24h volume ≥ $20,000,000 (highest requirement — panics need liquid markets)
- [ ] RVOL ≥ 3.0× (capitulation spike required)
- [ ] RSI (15m) ≤ 30 (not 35, not 28 — strictly ≤ 30)
- [ ] RSI (1h) ≤ 40 (1h must also confirm oversold macro weakness)
- [ ] 4h price change ≤ −2.5% (genuine 4h decline, not just a 15m dip)
- [ ] 4h EMA alignment: exempt — BEARISH 4h EMA is expected for a reversal setup
- [ ] 15m EMA stack: exempt — BEARISH 15m stack is expected
- [ ] Symbol not already in an open position
- [ ] Sector correlation guard passes

### Brain / decision.py Criteria

**Hard gates (any one blocks):**
- RSI ≥ 30 → "the bounce already started, too late" — blocked
- %B > 35 → price not near lower Bollinger Band — blocked

**3 Exhaustion Signals (need ≥ 2 out of 3 for grade B; ≥ 3 for grade A):**
1. RSI (15m) < 30 (deeply oversold)
2. %B ≤ 25% (price at lower Bollinger Band)
3. MACD histogram negative but magnitude SHRINKING (`|histogram| < |histogram_prev|`)

**Additional signals checked (informational, add to confluence count):**
4. RVOL ≥ 3.0× (capitulation volume spike)
5. Recent bearish candles showing decreasing sell volume (last 4 bars: latest bearish bar volume < earliest bearish bar volume)

REVERSAL requires grade A (≥ 3 exhaustion signals including the hard gates). Grade B does not qualify.

**BTC STRONG_BEARISH gate:** REVERSAL is the ONLY strategy that proceeds when BTC bias is `STRONG_BEARISH`. All other strategies are blocked at the Brain level. The logic is: `STRONG_BEARISH` BTC creates genuine panic selling that produces reversal setups.

**R:R minimum:** 3.0:1 — counter-trend trades have a lower base win rate and must compensate with larger reward.

**SL default:** 2.5% (vs 1.5% for momentum strategies) — reversal entries need room for the initial flush to complete below the entry.

### Active Regimes

All four regimes (`BULL_TRENDING`, `BEAR_TRENDING`, `RANGING`, `MIXED`) allow REVERSAL. In `BEAR_TRENDING` regime (where AGGRESSIVE is blocked), REVERSAL is the primary high-activity strategy. Sizing is:
- `BULL_TRENDING`: 1.0× multiplier
- `BEAR_TRENDING`: 0.5× multiplier
- `RANGING` / `MIXED`: 0.75× multiplier

---

## Market Condition Walk-Throughs

### Example 1: BULL_TRENDING Market

**Hypothetical SOL/USDT snapshot:**
- Price: $148.20
- 24h volume: $850M
- BTC 4h EMA: BULLISH (ema9 > ema21 > ema50 on 4h)
- BTC ADX (4h): 32.5
- BTC RSI (15m): 58
- Market breadth: 68% of symbols with bullish 4h EMA
- Breadth RSI > 50: 65%
- SOL RSI (15m): 53
- SOL RSI (1h): 61
- SOL RVOL: 2.3×
- SOL 4h change: +1.8%
- SOL 15m EMA stack: BULLISH
- SOL MACD histogram: +0.0042 (prev: +0.0031, growing)
- SOL VWAP: $146.10 (price 1.4% above VWAP)
- SOL ATR: $0.95 → sl_pct = 0.95/148.20 × 100 × 1.5 = 0.96% (use min 1.2%)

**Regime detection:**
- BTC 4h EMA BULLISH → +1 bull vote
- Breadth 68% ≥ 55% → +1 bull vote
- ADX 32.5 ≥ 25 + BTC BULLISH → +1 bull vote
- BTC RSI 58 ≥ 50 → +1 bull vote
- Breadth RSI 65% ≥ 55% → +1 bull vote
- **Total: 5 bull votes → BULL_TRENDING (confidence=100%)**
- position_size_multiplier = 1.0
- active_strategies = [CONSERVATIVE, AGGRESSIVE, REVERSAL]

**BTC bias:** EMA 1h BULLISH, MACD positive → `BULLISH_TAILWIND`

**Filter — CONSERVATIVE gates for SOL:**
- Volume $850M ≥ $10M ✓
- RVOL 2.3 ≥ 1.5 ✓
- RSI 53 in [40, 70] ✓
- RSI(1h) 61 ≤ 70 ✓
- 4h change +1.8% ≥ +0.3% ✓
- 15m EMA stack BULLISH (not BEARISH) ✓
- 4h EMA alignment: assume BULLISH (not blocked)
- **All Filter gates pass. filter_score = min(2.3/4, 1)×40 + 30 + (1-|53-55|/45)×20 + 10 = 23 + 30 + 19.1 + 10 = 82.1**

**Filter — AGGRESSIVE gates for SOL:** Also passes (lower thresholds).

**Brain / decision.py — CONSERVATIVE verdict:**

Hard gates: RSI 53 in [45,65] ✓ / 1h trend from EMA BULLISH ✓

5 signals:
1. Price $148.20 > VWAP $146.10 → PRO
2. 15m EMA BULLISH → PRO
3. MACD histogram +0.0042 > +0.0031 (growing) → PRO
4. RSI 53 in [45,65] → PRO
5. RVOL 2.3 ≥ 2.0 → PRO

All 5 signals: grade A. R:R check: sl_pct=1.2%, tp_pct=1.2×2.5=3.0%, R:R=2.5 ✓

**Verdict: BUY (CONSERVATIVE)**

SL: $148.20 × (1 - 0.012) = $146.42
TP: $148.20 × (1 + 0.030) = $152.65

**Kelly position sizing:**
Assume: win_rate=0.62, avg_win=2.8%, avg_loss=1.4%, base_risk=1%, confidence=83, drawdown=0%

```
b = 2.8 / 1.4 = 2.0
kelly_f = (0.62 * 2.0 - 0.38) / 2.0 = (1.24 - 0.38) / 2.0 = 0.43
kelly_f capped at 1.0: 0.43
fractional = 0.43 * 0.25 = 0.1075
scale = 0.3 + (0.1075 / 0.25) * 1.2 = 0.3 + 0.516 = 0.816
confidence_mult = 0.5 + (83/95) * 0.75 = 0.5 + 0.655 = 1.155 (capped at 95→ same)
dd_mult = 1.0 (drawdown 0%)
regime_mult = 1.0 (BULL_TRENDING)

risk_pct = 0.01 * 0.816 * 1.155 * 1.0 * 1.0 = 0.00943 (0.943%)
bounded: max(0.002, min(0.025, 0.00943)) = 0.943%
```

Assume balance = $1,000 USDT:
- risk_amount = $1,000 × 0.00943 = $9.43
- position_size = $9.43 / (1.2/100) = $785.83 notional
- max_notional = $1,000 × 3 = $3,000 → $785.83 is within bounds
- margin = $785.83 / 3 = $261.94
- entry_fee = $785.83 × 0.0005 = $0.39
- qty = $785.83 / $148.20 = 5.302 SOL

**Trade:** Long 5.302 SOL at $148.24 (entry with 0.02% slippage), SL $146.42, TP $152.65, notional $785.83

---

### Example 2: BEAR_TRENDING Market

**Hypothetical SOL/USDT snapshot:**
- Price: $89.40
- BTC 4h EMA: BEARISH (ema9 < ema21 < ema50)
- BTC ADX (4h): 38.0
- BTC RSI (15m): 33
- Market breadth: 22% bullish 4h EMA
- Breadth RSI > 50: 28%
- SOL RSI (15m): 24
- SOL RSI (1h): 31
- SOL RVOL: 4.1×
- SOL 4h change: −4.8%
- SOL %B: 8% (near lower BB)
- SOL MACD histogram: −0.0085 (prev: −0.0121, shrinking)

**Regime detection:**
- BTC 4h BEARISH → +1 bear vote
- Breadth 22% ≤ 40% → +1 bear vote
- ADX 38 ≥ 25 + BTC BEARISH → +1 bear vote
- BTC RSI 33 < 50 → +1 bear vote
- Breadth RSI 28% < 45% → +1 bear vote
- **Total: 5 bear votes → BEAR_TRENDING (confidence=100%)**
- position_size_multiplier = 0.5
- active_strategies = [CONSERVATIVE, REVERSAL]

**BTC bias:** EMA 1h BEARISH, EMA 15m BEARISH, RSI 33 < 40 → `STRONG_BEARISH`

**Brain BTC gate:** `STRONG_BEARISH` + strategy is not REVERSAL → **blocks CONSERVATIVE, blocks AGGRESSIVE**. Only REVERSAL proceeds.

**Filter — REVERSAL gates for SOL:**
- Volume: assume $180M ≥ $20M ✓
- RVOL 4.1 ≥ 3.0 ✓
- RSI(15m) 24 ≤ 30 ✓
- RSI(1h) 31 ≤ 40 ✓
- 4h change −4.8% ≤ −2.5% ✓
- **All Filter gates pass.**

**Brain / decision.py — REVERSAL verdict:**

Hard gates: RSI 24 < 30 ✓ / %B 8% ≤ 35% ✓

Exhaustion signals:
1. RSI 24 < 30 → PRO (deeply oversold exhaustion)
2. %B 8% ≤ 25% → PRO (at lower BB support)
3. MACD histogram −0.0085, prev −0.0121: |−0.0085| < |−0.0121| → shrinking → PRO (sellers exhausting)

All 3 exhaustion signals: grade A. R:R check: sl_pct = max(ATR×1.5, 1.2). Assume ATR $1.80 → sl_pct = (1.80/89.40)×100×1.5 = 3.02%. tp_pct = 3.02 × 3.0 = 9.06%. R:R = 3.0 ✓

**Verdict: BUY (REVERSAL)**

SL: $89.40 × (1 - 0.0302) = $86.70
TP: $89.40 × (1 + 0.0906) = $97.50

**Kelly with regime multiplier:**
Assume win_rate=0.55, avg_win=5.2%, avg_loss=2.1%, confidence=75, drawdown=2%, regime_mult=0.5

```
b = 5.2 / 2.1 = 2.476
kelly_f = (0.55 × 2.476 - 0.45) / 2.476 = (1.362 - 0.45) / 2.476 = 0.368
fractional = 0.368 × 0.25 = 0.092
scale = 0.3 + (0.092 / 0.25) × 1.2 = 0.3 + 0.442 = 0.742
confidence_mult = 0.5 + (75/95) × 0.75 = 0.5 + 0.592 = 1.092
dd_mult = 1.0 (drawdown 2% < 5%)
regime_mult = 0.5

risk_pct = 0.01 × 0.742 × 1.092 × 1.0 × 0.5 = 0.00405 (0.405%)
bounded: max(0.002, 0.00405) = 0.405%
```

Notional = $1,000 × 0.00405 / (3.02/100) = $134.10 — small position reflecting the bear regime half-sizing.

---

### Example 3: RANGING Market

**Hypothetical SOL/USDT snapshot:**
- Price: $112.50, range between $110–$115 for 3 days
- BTC 4h EMA: RECOVERING (short-term recovery, below 50 EMA)
- BTC ADX (4h): 13.5 (below 20 — forces RANGING regardless of votes)
- BTC RSI (15m): 52
- Market breadth: 48% bullish
- SOL RSI (15m): 48
- SOL RSI (1h): 52
- SOL RVOL: 1.7×
- SOL 4h change: +0.5%
- SOL VWAP: $112.20

**Regime detection:**
- ADX 13.5 < 20 → **forces RANGING regardless of vote count**
- confidence = max(30, int((1 - 13.5/20) × 80)) = max(30, 54) = 54%
- position_size_multiplier = 0.75
- active_strategies = [CONSERVATIVE, REVERSAL]

**AGGRESSIVE is excluded** — breakout strategies fail in ranges.

**Filter — CONSERVATIVE gates for SOL:** RVOL 1.7 ≥ 1.5 ✓, RSI 48 in [40,70] ✓, 4h change +0.5% ≥ +0.3% ✓. Passes.

**Brain / decision.py — CONSERVATIVE:**

5 signals:
1. Price $112.50 > VWAP $112.20 → PRO (barely)
2. 15m EMA: assume MIXED → CON
3. MACD: assume histogram slightly positive but flat → partial (3 pts only at filter score; in decision: positive but not growing → partial PRO)
4. RSI 48 in [45,65] → PRO
5. RVOL 1.7 ≥ 2.0 → CON

Only 2–3 clear PRO signals. Grade B, but CONSERVATIVE requires grade A (≥ 4 signals). **Verdict: WAIT** — "Only 2/3 required signals confirmed."

**What a typical day looks like:** In RANGING, most CONSERVATIVE setups produce WAIT verdicts. The brain finds 1–2 BUY signals per day, mostly from symbols that are breaking out of the range relative to the overall market (high RVOL on a consolidation breakout). REVERSAL setups do not fire unless a symbol has sold off to the lower range boundary with RSI < 30. Position sizes are 75% of normal.

---

### Example 4: MIXED Market

**Hypothetical SOL/USDT snapshot:**
- BTC 4h EMA: WEAKENING (ema9 < ema21 but ema21 > ema50)
- BTC ADX (4h): 22.0 (above 20, so not forced RANGING)
- BTC RSI (15m): 48
- Market breadth: 43% bullish
- Breadth RSI > 50: 46%

**Regime detection:**
- BTC WEAKENING → +1 bear vote
- Breadth 43%: not ≥ 55% (no bull vote), not ≤ 40% (no bear vote)
- ADX 22 ≥ 25? No → no ADX vote
- BTC RSI 48 < 50 → +1 bear vote
- Breadth RSI 46% < 45%? No → no vote
- **Total: 0 bull votes, 2 bear votes → MIXED (confidence=40%)**
- position_size_multiplier = 0.75
- active_strategies = [CONSERVATIVE, REVERSAL]

**BTC bias:** EMA 1h WEAKENING (bearish set), MACD neutral → `BEARISH_HEADWIND`

**Brain BEARISH_HEADWIND effect:** `max_open` halved (e.g., from 4 to `ceil(4/2) = 2`). min_confluence for CONSERVATIVE raised from 3 to 4 (requiring grade A instead of accepting B).

**What GPT/decision.py is likely to say:** With BEARISH_HEADWIND raising the bar and only 2 effective position slots, the system becomes highly selective. Most CONSERVATIVE setups need all 5 signals to fire. REVERSAL setups require genuine capitulation. Expect 0–1 new positions opened per day.

**What a typical trading day looks like:** The bot is cautious. It scans the full universe every 10 seconds but most candidates fail the BEARISH_HEADWIND elevated confluence bar. When a CONSERVATIVE trade does open, it is sized at 75% via regime multiplier. There is a real chance of zero new positions opened during a MIXED day.
