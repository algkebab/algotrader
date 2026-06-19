"""
Look-ahead-safe feature engineering for the ML signal model.

Pure-Python (no numpy) so it can run identically in the live Brain service and
in the offline training script — guaranteeing train/serve feature parity.

CRITICAL CONTRACT: every function here consumes only *past* candles relative to
the decision point. Candles are ordered oldest→newest and the caller must pass a
slice that ends at (and includes) the most recently CLOSED bar. No function ever
peeks at a forming or future bar. This is what eliminates look-ahead bias.

Candle format (matches the codebase): [ts_ms, open, high, low, close, volume]
"""

import math

# Bars-per-year constants for annualising realised volatility.
# 15m bars: 4 per hour * 24 * 365 = 35,040
BARS_PER_YEAR_15M = 35_040
BARS_PER_YEAR_1H = 8_760
BARS_PER_YEAR_4H = 2_190

# Canonical, ordered feature names. The training script and the live predictor
# MUST agree on this exact order — the model is indexed positionally.
# Keep additions append-only and retrain when this list changes.
FEATURE_NAMES = [
    "ret_1",            # last log return (15m)
    "ret_4",            # 1h log return (4x15m)
    "ret_16",           # 4h log return (16x15m)
    "rvol_20",          # realised volatility, 20-bar rolling (annualised)
    "rvol_50",          # realised volatility, 50-bar rolling (annualised)
    "vol_of_vol",       # std of rolling realised vol (volatility clustering)
    "parkinson_20",     # Parkinson high-low volatility estimator, 20-bar
    "mom_z_20",         # z-score of 20-bar momentum
    "mom_z_50",         # z-score of 50-bar momentum
    "rsi_norm",         # (RSI-50)/50 in [-1,1]
    "macd_hist_norm",   # MACD histogram / price
    "ema_align_15m",    # encoded 15m EMA alignment in [-1,1]
    "ema_align_1h",     # encoded 1h EMA alignment in [-1,1]
    "ema_align_4h",     # encoded 4h EMA alignment in [-1,1]
    "bb_pct_b",         # Bollinger %B in [0,1] (clipped)
    "bb_bandwidth",     # Bollinger bandwidth (volatility regime)
    "dist_vwap",        # (price - vwap)/vwap
    "obi_proxy",        # order-flow imbalance proxy from bar internals
    "volume_z_20",      # z-score of volume over 20 bars
    "rvol_ratio",       # current bar vol / 50-bar avg vol (relative volume)
    "xs_mom_rank",      # cross-sectional momentum rank in [0,1] (0.5 = neutral)
    "frac_diff",        # fractionally-differenced log price (stationary, keeps memory)
    # --- positioning / market-structure features (not in OHLCV) ---
    "funding_rate",     # latest 8h perpetual funding rate (raw, e.g. 0.0001)
    "funding_bias",     # mean of last 6 funding rates — persistent sentiment direction
    "oi_change_pct",    # % change in open interest over the last 4h period
]

# EMA alignment string -> numeric encoding (bullish positive, bearish negative)
_EMA_ALIGN_ENCODING = {
    "BULLISH": 1.0,
    "RECOVERING": 0.5,
    "MIXED": 0.0,
    "WEAKENING": -0.5,
    "BEARISH": -1.0,
}


def _closes(candles: list) -> list:
    return [float(c[4]) for c in candles]


def log_returns(closes: list) -> list:
    """Log returns log(c_t / c_{t-1}). Returns list of length len(closes)-1."""
    out = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        cur = closes[i]
        if prev > 0 and cur > 0:
            out.append(math.log(cur / prev))
        else:
            out.append(0.0)
    return out


def realized_volatility(candles: list, window: int = 20,
                        bars_per_year: int = BARS_PER_YEAR_15M) -> float:
    """Annualised realised volatility = sqrt(mean(r^2)) over the last `window`
    log returns, scaled by sqrt(bars_per_year). Returns 0.0 if insufficient data.

    This is true realised volatility (RMS of log returns), NOT relative volume.
    """
    closes = _closes(candles)
    if len(closes) < window + 1:
        return 0.0
    rets = log_returns(closes)[-window:]
    if not rets:
        return 0.0
    mean_sq = sum(r * r for r in rets) / len(rets)
    return math.sqrt(mean_sq) * math.sqrt(bars_per_year)


def rolling_realized_vol_series(candles: list, window: int = 20,
                                bars_per_year: int = BARS_PER_YEAR_15M) -> list:
    """Series of rolling realised volatility, one value per window step.
    Used to compute vol-of-vol (clustering signal)."""
    closes = _closes(candles)
    rets = log_returns(closes)
    if len(rets) < window:
        return []
    out = []
    scale = math.sqrt(bars_per_year)
    for i in range(window, len(rets) + 1):
        w = rets[i - window:i]
        mean_sq = sum(r * r for r in w) / len(w)
        out.append(math.sqrt(mean_sq) * scale)
    return out


def parkinson_volatility(candles: list, window: int = 20,
                         bars_per_year: int = BARS_PER_YEAR_15M) -> float:
    """Parkinson high-low range volatility estimator (more efficient than
    close-to-close). sigma^2 = 1/(4 ln 2) * mean(ln(H/L)^2). Annualised."""
    if len(candles) < window:
        return 0.0
    w = candles[-window:]
    acc = 0.0
    n = 0
    for c in w:
        high = float(c[2])
        low = float(c[3])
        if high > 0 and low > 0:
            acc += math.log(high / low) ** 2
            n += 1
    if n == 0:
        return 0.0
    var = acc / (4.0 * math.log(2.0) * n)
    return math.sqrt(var) * math.sqrt(bars_per_year)


def _mean_std(values: list) -> tuple:
    n = len(values)
    if n == 0:
        return 0.0, 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return mean, math.sqrt(var)


def momentum_zscore(closes: list, lookback: int, z_window: int = 100) -> float:
    """Z-score of `lookback`-bar momentum relative to its own recent distribution.
    Normalises momentum so it's comparable across assets and volatility regimes."""
    if len(closes) < lookback + z_window + 1:
        # Fall back to a shorter window if not enough history
        z_window = max(20, len(closes) - lookback - 1)
        if z_window < 20 or len(closes) < lookback + z_window + 1:
            return 0.0
    moms = []
    for i in range(len(closes) - z_window, len(closes)):
        if i - lookback >= 0 and closes[i - lookback] > 0:
            moms.append((closes[i] - closes[i - lookback]) / closes[i - lookback])
    if len(moms) < 2:
        return 0.0
    mean, std = _mean_std(moms)
    if std == 0:
        return 0.0
    current = moms[-1]
    z = (current - mean) / std
    # clip to a sane range
    return max(-5.0, min(5.0, z))


def volume_zscore(candles: list, window: int = 20) -> float:
    """Z-score of the latest CLOSED bar's volume vs the prior `window` bars."""
    if len(candles) < window + 1:
        return 0.0
    vols = [float(c[5]) for c in candles[-(window + 1):]]
    current = vols[-1]
    history = vols[:-1]
    mean, std = _mean_std(history)
    if std == 0:
        return 0.0
    return max(-5.0, min(5.0, (current - mean) / std))


def order_book_imbalance_proxy(candles: list, window: int = 5) -> float:
    """Order-flow imbalance proxy from bar internals (we only have OHLCV, not L2).

    For each bar, close position within range maps to buy/sell pressure:
        cp = (close - low) / (high - low)  in [0,1]   (1 = closed on highs)
    Weight by volume and average over `window` bars, then center to [-1,1].
    A positive value => buying pressure dominated recent bars.
    """
    if len(candles) < window:
        return 0.0
    w = candles[-window:]
    num = 0.0
    den = 0.0
    for c in w:
        high = float(c[2])
        low = float(c[3])
        close = float(c[4])
        vol = float(c[5])
        rng = high - low
        if rng <= 0 or vol <= 0:
            continue
        cp = (close - low) / rng  # 0..1
        num += (2.0 * cp - 1.0) * vol  # -1..1 weighted by volume
        den += vol
    if den == 0:
        return 0.0
    return max(-1.0, min(1.0, num / den))


def frac_diff_last(closes: list, d: float = 0.4, width: int = 50,
                   threshold: float = 1e-4) -> float:
    """Fractionally-differenced log price at the latest bar (fixed-width window).

    Fractional differentiation (López de Prado) removes the unit root to make the
    series stationary while preserving long-memory that integer differencing
    destroys. Returns the single most-recent value of the FFD series.

    d in (0,1): 0 = raw (non-stationary), 1 = standard differencing (memoryless).
    """
    if len(closes) < 2:
        return 0.0
    # Build FFD weights until they fall below threshold or hit width cap
    weights = [1.0]
    k = 1
    while k < width:
        w = -weights[-1] * (d - k + 1) / k
        if abs(w) < threshold:
            break
        weights.append(w)
        k += 1
    n = len(weights)
    log_px = [math.log(c) if c > 0 else 0.0 for c in closes]
    if len(log_px) < n:
        n = len(log_px)
        weights = weights[:n]
    window = log_px[-n:]
    # weights[0] applies to most recent, so reverse-align
    val = 0.0
    for i in range(n):
        val += weights[i] * window[-(i + 1)]
    return val


def funding_and_positioning(
    funding_rates: list,
    oi_series: list,
) -> dict:
    """Compute positioning features from funding/OI/basis time series.

    Each series is a list of (timestamp_ms, value_float) sorted ascending.
    Returns neutral zeros on missing/failed data so the model degrades gracefully.
    """
    out = {"funding_rate": 0.0, "funding_bias": 0.0, "oi_change_pct": 0.0}
    if funding_rates:
        recent = [r for _, r in funding_rates[-6:]]
        out["funding_rate"] = recent[-1]
        out["funding_bias"] = sum(recent) / len(recent)
    if len(oi_series) >= 2:
        oi_now = oi_series[-1][1]
        oi_prev = oi_series[-2][1]
        out["oi_change_pct"] = (oi_now - oi_prev) / oi_prev if oi_prev > 0 else 0.0
    return out


def cross_sectional_rank(value: float, peer_values: list) -> float:
    """Percentile rank of `value` within `peer_values` (inclusive), in [0,1].
    Returns 0.5 (neutral) when there are no peers. Used for cross-sectional
    momentum: which assets are strongest right now relative to the basket."""
    if not peer_values:
        return 0.5
    below = sum(1 for v in peer_values if v < value)
    equal = sum(1 for v in peer_values if v == value)
    n = len(peer_values)
    # midrank for ties; bounded in [0,1]
    return (below + 0.5 * equal) / n


def _encode_alignment(ema_stack) -> float:
    if not ema_stack:
        return 0.0
    return _EMA_ALIGN_ENCODING.get(ema_stack.get("alignment", "MIXED"), 0.0)


def build_features(
    candles_15m: list,
    candles_1h: list,
    candles_4h: list,
    rsi: float = None,
    indicators: dict = None,
    xs_momentum_rank: float = 0.5,
    frac_diff_d: float = 0.4,
    positioning: dict = None,
) -> dict:
    """Assemble the full feature vector for one (symbol, timestamp).

    All inputs must be sliced so the last element is the latest CLOSED bar.
    `indicators` is the same dict the Filter/Brain already compute (vwap, atr,
    ema_stack_*, bollinger_15m, macd_15m) — reused to avoid recomputation.
    `xs_momentum_rank` is supplied by the caller from the peer universe.

    Returns {feature_name: float} for every name in FEATURE_NAMES.
    """
    ind = indicators or {}
    closes = _closes(candles_15m)
    price = closes[-1] if closes else 0.0

    def lr(n):
        if len(closes) < n + 1 or closes[-(n + 1)] <= 0 or price <= 0:
            return 0.0
        return math.log(price / closes[-(n + 1)])

    rvol_series = rolling_realized_vol_series(candles_15m, window=20)
    vol_of_vol = _mean_std(rvol_series[-20:])[1] if len(rvol_series) >= 2 else 0.0

    rsi_v = float(rsi) if rsi is not None else 50.0
    macd = ind.get("macd_15m") or {}
    macd_hist = float(macd.get("histogram") or 0.0)
    bb = ind.get("bollinger_15m") or {}
    vwap = ind.get("vwap")
    pct_b = bb.get("pct_b")
    bandwidth = bb.get("bandwidth")

    feats = {
        "ret_1": lr(1),
        "ret_4": lr(4),
        "ret_16": lr(16),
        "rvol_20": realized_volatility(candles_15m, 20),
        "rvol_50": realized_volatility(candles_15m, 50),
        "vol_of_vol": vol_of_vol,
        "parkinson_20": parkinson_volatility(candles_15m, 20),
        "mom_z_20": momentum_zscore(closes, 20),
        "mom_z_50": momentum_zscore(closes, 50),
        "rsi_norm": (rsi_v - 50.0) / 50.0,
        "macd_hist_norm": (macd_hist / price) if price > 0 else 0.0,
        "ema_align_15m": _encode_alignment(ind.get("ema_stack_15m")),
        "ema_align_1h": _encode_alignment(ind.get("ema_stack_1h")),
        "ema_align_4h": _encode_alignment(ind.get("ema_stack_4h")),
        "bb_pct_b": max(0.0, min(1.0, (pct_b / 100.0))) if pct_b is not None else 0.5,
        "bb_bandwidth": float(bandwidth) if bandwidth is not None else 0.0,
        "dist_vwap": ((price - vwap) / vwap) if (vwap and vwap > 0) else 0.0,
        "obi_proxy": order_book_imbalance_proxy(candles_15m, 5),
        "volume_z_20": volume_zscore(candles_15m, 20),
        "rvol_ratio": _rvol_ratio(candles_15m, 50),
        "xs_mom_rank": float(xs_momentum_rank),
        "frac_diff": frac_diff_last(closes, d=frac_diff_d),
    }
    pos = positioning or {}
    feats["funding_rate"] = float(pos.get("funding_rate", 0.0))
    feats["funding_bias"] = float(pos.get("funding_bias", 0.0))
    feats["oi_change_pct"] = float(pos.get("oi_change_pct", 0.0))
    return feats


def _rvol_ratio(candles: list, period: int = 50) -> float:
    """Relative volume: latest closed bar volume / average of prior `period` bars."""
    if len(candles) < period + 1:
        return 1.0
    current = float(candles[-1][5])
    prior = [float(c[5]) for c in candles[-(period + 1):-1]]
    avg = sum(prior) / len(prior) if prior else 0.0
    return (current / avg) if avg > 0 else 1.0


def features_to_vector(feats: dict) -> list:
    """Order a feature dict into the canonical positional vector for the model."""
    return [float(feats.get(name, 0.0)) for name in FEATURE_NAMES]
