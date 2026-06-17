"""
Pure indicator functions shared by Filter (live) and Backtester (historical replay).
No Redis, no DB, no I/O — these are stateless computations only.
"""

from datetime import datetime, timezone


def compute_rsi(candles: list, period: int = 14) -> float:
    """Compute RSI using Wilder's smoothing — matches TradingView exactly.

    Seeds avg_gain/avg_loss with the SMA of the first `period` moves,
    then applies: avg = (prev_avg * (period-1) + current) / period.
    Returns 50 (neutral) when there is insufficient data.
    """
    if len(candles) < period + 1:
        return 50.0

    closes = [c[4] for c in candles]
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def compute_ema(prices: list, period: int) -> list:
    """Compute Exponential Moving Average seeded with SMA.

    Returns a list of EMA values starting from index `period-1` of the
    input series. Returns [] if there is insufficient data.
    """
    if len(prices) < period:
        return []
    k = 2.0 / (period + 1)
    ema_values = [sum(prices[:period]) / period]
    for price in prices[period:]:
        ema_values.append(price * k + ema_values[-1] * (1 - k))
    return ema_values


def compute_vwap(candles: list) -> float | None:
    """Compute Volume-Weighted Average Price over all provided candles.

    Uses typical price = (high + low + close) / 3.
    Returns None when candles are empty or total volume is zero.
    """
    if not candles:
        return None
    total_tv = sum((c[2] + c[3] + c[4]) / 3.0 * c[5] for c in candles)
    total_vol = sum(c[5] for c in candles)
    return total_tv / total_vol if total_vol > 0 else None


def compute_atr(candles: list, period: int = 14) -> float | None:
    """Compute Average True Range using Wilder's smoothing (industry standard).

    Seeds with SMA of the first `period` true ranges, then applies:
        ATR(t) = (ATR(t-1) × (period-1) + TR(t)) / period
    True Range = max(H-L, |H-prev_C|, |L-prev_C|).
    Returns None when there are fewer than `period + 1` candles.
    """
    if len(candles) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(candles)):
        high, low, prev_close = candles[i][2], candles[i][3], candles[i - 1][4]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def compute_bollinger_bands(closes: list, period: int = 20, num_std: float = 2.0) -> dict | None:
    """Compute Bollinger Bands on the most recent `period` closes.

    Returns a dict with keys: upper, middle, lower, pct_b, bandwidth.
    pct_b: 0% = at lower band, 100% = at upper band.
    bandwidth: (upper - lower) / middle * 100 — squeeze when < 2%.
    Returns None when there is insufficient data.
    """
    if len(closes) < period:
        return None
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std = variance ** 0.5
    upper = middle + num_std * std
    lower = middle - num_std * std
    band_range = upper - lower
    pct_b = (closes[-1] - lower) / band_range * 100 if band_range > 0 else 50.0
    bandwidth = band_range / middle * 100 if middle > 0 else 0.0
    return {
        "upper": round(upper, 8),
        "middle": round(middle, 8),
        "lower": round(lower, 8),
        "pct_b": round(pct_b, 1),
        "bandwidth": round(bandwidth, 2),
    }


def compute_macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    """Compute MACD line, signal line, and histogram.

    Returns dict with: macd, signal_line, histogram, histogram_prev.
    histogram_prev enables momentum direction detection (growing vs. shrinking).
    Returns None when there is insufficient data (need >= slow + signal prices).
    """
    if len(closes) < slow + signal:
        return None
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)
    if not ema_fast or not ema_slow:
        return None
    offset = len(ema_fast) - len(ema_slow)
    macd_line = [ema_fast[i + offset] - ema_slow[i] for i in range(len(ema_slow))]
    signal_ema = compute_ema(macd_line, signal)
    if not signal_ema:
        return None
    histogram = macd_line[-1] - signal_ema[-1]
    histogram_prev = macd_line[-2] - signal_ema[-2] if len(signal_ema) >= 2 else None
    return {
        "macd": round(macd_line[-1], 8),
        "signal_line": round(signal_ema[-1], 8),
        "histogram": round(histogram, 8),
        "histogram_prev": round(histogram_prev, 8) if histogram_prev is not None else None,
    }


def compute_ema_stack(closes: list) -> dict | None:
    """Compute EMA 9/21/50 and describe their alignment as a trend label.

    Returns dict with: ema9, ema21, ema50, alignment, description.
    Returns None when there is insufficient data for EMA 50.
    """
    ema9 = compute_ema(closes, 9)
    ema21 = compute_ema(closes, 21)
    ema50 = compute_ema(closes, 50)
    if not ema9 or not ema21 or not ema50:
        return None
    e9, e21, e50 = ema9[-1], ema21[-1], ema50[-1]
    if e9 > e21 > e50:
        alignment, description = "BULLISH", "EMA9 > EMA21 > EMA50 (full bullish stack)"
    elif e9 < e21 < e50:
        alignment, description = "BEARISH", "EMA9 < EMA21 < EMA50 (full bearish stack)"
    elif e9 > e21 and e21 < e50:
        alignment, description = "RECOVERING", "EMA9 > EMA21 < EMA50 (short-term recovery, below long-term trend)"
    elif e9 < e21 and e21 > e50:
        alignment, description = "WEAKENING", "EMA9 < EMA21 > EMA50 (short-term weakening, still above long-term)"
    else:
        alignment, description = "MIXED", "Mixed EMA stack (no clear trend)"
    return {
        "ema9": round(e9, 8),
        "ema21": round(e21, 8),
        "ema50": round(e50, 8),
        "alignment": alignment,
        "description": description,
    }


def compute_adx(candles: list, period: int = 14) -> float | None:
    """Compute ADX (Average Directional Index) using Wilder's smoothing.

    ADX measures trend *strength*, not direction: >25 = trending, <20 = ranging.
    Returns None when there is insufficient data (need >= period * 2 + 1 candles).
    """
    if len(candles) < period * 2 + 1:
        return None
    plus_dms, minus_dms, trs = [], [], []
    for i in range(1, len(candles)):
        h, l, pc = candles[i][2], candles[i][3], candles[i - 1][4]
        ph, pl = candles[i - 1][2], candles[i - 1][3]
        up, dn = h - ph, pl - l
        plus_dms.append(up if up > dn and up > 0 else 0.0)
        minus_dms.append(dn if dn > up and dn > 0 else 0.0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    tr14  = sum(trs[:period])
    pdm14 = sum(plus_dms[:period])
    mdm14 = sum(minus_dms[:period])
    dx_values = []
    for i in range(period, len(trs)):
        tr14  = tr14  - tr14  / period + trs[i]
        pdm14 = pdm14 - pdm14 / period + plus_dms[i]
        mdm14 = mdm14 - mdm14 / period + minus_dms[i]
        if tr14 == 0:
            continue
        pdi = 100 * pdm14 / tr14
        mdi = 100 * mdm14 / tr14
        denom = pdi + mdi
        if denom == 0:
            continue
        dx_values.append(100 * abs(pdi - mdi) / denom)
    if len(dx_values) < period:
        return None
    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = (adx * (period - 1) + dx) / period
    return round(adx, 2)


def compute_recent_change(candles_15m: list, lookback: int = 16) -> float:
    """Price change over the last `lookback` 15m bars (default 16 = 4 hours).

    Uses closed candles: candles[-2] (last closed) vs candles[-(lookback+2)] (start).
    Returns 0.0 when there are insufficient candles.
    """
    needed = lookback + 2
    if len(candles_15m) < needed:
        return 0.0
    price_now = candles_15m[-2][4]
    price_then = candles_15m[-(lookback + 2)][4]
    if price_then == 0:
        return 0.0
    return round((price_now - price_then) / price_then * 100, 2)


def compute_rvol(candles: list, period: int = 50) -> float:
    """Standard RVOL: current bar volume / average volume of prior `period` bars.

    Uses candles[-2] (last CLOSED bar) — candles[-1] is the currently-forming bar.
    Returns 0.0 when there is insufficient data.
    """
    if len(candles) < period + 2:
        return 0.0
    current_vol = candles[-2][5]
    avg_vol = sum(c[5] for c in candles[-(period + 2):-2]) / period
    return round(current_vol / avg_vol, 2) if avg_vol > 0 else 0.0


def compute_all_indicators(
    candles_15m: list,
    candles_1h: list,
    candles_4h: list | None = None,
    as_of_ts_ms: int | None = None,
) -> dict:
    """Orchestrate all technical indicator computations.

    Returns a flat dict of indicator values ready to merge into the candidate payload.
    All fields are None when candles are missing or insufficient.

    as_of_ts_ms: unix timestamp in milliseconds for VWAP session reset calculation.
                 None = use datetime.now(utc) — live behavior.
                 Provided = use that timestamp — backtest behavior.
    """
    result = {}

    if candles_15m:
        closes_15m = [c[4] for c in candles_15m]

        # Session VWAP: filter candles to today 00:00 UTC (institutional daily reset).
        # Falls back to last 32 candles early in the session (< 10 today candles).
        if as_of_ts_ms is not None:
            ref_dt = datetime.fromtimestamp(as_of_ts_ms / 1000, tz=timezone.utc)
        else:
            ref_dt = datetime.now(timezone.utc)
        today_midnight_ms = int(
            ref_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
        )
        session_candles = [c for c in candles_15m if c[0] >= today_midnight_ms]
        if len(session_candles) < 10:
            session_candles = candles_15m[-32:]
        result["vwap"] = compute_vwap(session_candles)
        result["atr"] = compute_atr(candles_15m, period=14)
        result["ema_stack_15m"] = compute_ema_stack(closes_15m)
        result["bollinger_15m"] = compute_bollinger_bands(closes_15m)
        result["macd_15m"] = compute_macd(closes_15m)
    else:
        result.update({"vwap": None, "atr": None, "ema_stack_15m": None,
                       "bollinger_15m": None, "macd_15m": None})

    if candles_1h:
        closes_1h = [c[4] for c in candles_1h]
        result["ema_stack_1h"] = compute_ema_stack(closes_1h)
    else:
        result["ema_stack_1h"] = None

    if candles_4h:
        closes_4h = [c[4] for c in candles_4h]
        result["ema_stack_4h"] = compute_ema_stack(closes_4h)
    else:
        result["ema_stack_4h"] = None

    return result


def score_candidate(candidate: dict, strategy_name: str) -> float:
    """Score a candidate 0–100. Brain processes highest-scored candidates first.

    Dimensions:
      RVOL (40 pts) — volume participation, capped at 4× to avoid outliers dominating
      EMA alignment (30 pts) — trend quality
      RSI quality (20 pts) — momentum sweet spot vs overbought/oversold
      MACD histogram (10 pts) — momentum direction and strength
    """
    score = 0.0

    rvol = candidate.get('rvol') or 0
    score += min(rvol / 4.0, 1.0) * 40

    ema_alignment = (candidate.get('ema_stack_15m') or {}).get('alignment', 'MIXED')
    ema_pts = {'BULLISH': 30, 'RECOVERING': 20, 'MIXED': 10, 'WEAKENING': 5, 'BEARISH': 0}
    score += ema_pts.get(ema_alignment, 10)

    rsi = candidate.get('rsi') or 50
    if strategy_name == 'REVERSAL':
        score += max(0.0, (30 - rsi) / 30 * 20)
    else:
        score += max(0.0, (1.0 - abs(rsi - 55) / 45) * 20)

    macd = candidate.get('macd_15m') or {}
    hist = macd.get('histogram') or 0
    hist_prev = macd.get('histogram_prev') or 0
    if hist > 0 and hist > hist_prev:
        score += 10
    elif hist > 0:
        score += 5

    return round(score, 1)
