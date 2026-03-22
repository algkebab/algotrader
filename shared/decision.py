"""
Code-based trading decision engine.

Mirrors the GPT prompt rules from Brain.get_ai_verdict() using deterministic logic.
Used for backtesting and optional live use when decision_mode="code".

Output schema is identical to get_ai_verdict() — same JSON fields — so nothing
downstream changes when switching between GPT and code modes.
"""

import math
import uuid


# ── Strategy profiles ──────────────────────────────────────────────────────────

STRATEGY_PROFILES = {
    "CONSERVATIVE": {
        "min_confluence": 3,   # out of 5 signals
        "min_rr": 2.5,
        "rsi_min": 45,
        "rsi_max": 65,
        "rvol_min": 2.0,
        "sl_default_pct": 1.5,
    },
    "AGGRESSIVE": {
        "min_confluence": 2,   # out of 5 signals
        "min_rr": 2.0,
        "rsi_min": 35,
        "rsi_max": 85,
        "rvol_min": 1.5,
        "sl_default_pct": 1.5,
    },
    "REVERSAL": {
        "min_confluence": 2,   # out of 3 exhaustion signals
        "min_rr": 3.0,
        "rsi_min": 0,
        "rsi_max": 30,
        "rvol_min": 3.0,
        "sl_default_pct": 2.5,
    },
}

_BULLISH_ALIGNMENTS = {"BULLISH", "RECOVERING"}
_BEARISH_ALIGNMENTS = {"BEARISH", "WEAKENING"}


# ── Internal helpers ────────────────────────────────────────────────────────────

def _detect_trend_1h(candles_1h, ema_stack_1h=None) -> str:
    """Derive 1h trend from EMA stack if available, otherwise from price structure."""
    if ema_stack_1h:
        align = ema_stack_1h.get("alignment", "MIXED")
        if align in _BULLISH_ALIGNMENTS:
            return "BULLISH"
        if align in _BEARISH_ALIGNMENTS:
            return "BEARISH"
        return "NEUTRAL"

    if not candles_1h or len(candles_1h) < 6:
        return "NEUTRAL"

    closes = [float(c[4]) for c in candles_1h[-24:]]
    mid = len(closes) // 2
    first_avg = sum(closes[:mid]) / mid
    second_avg = sum(closes[mid:]) / (len(closes) - mid)
    pct = (second_avg - first_avg) / first_avg * 100
    if pct > 1.0:
        return "BULLISH"
    if pct < -1.0:
        return "BEARISH"
    return "NEUTRAL"


def _detect_setup_15m(candles_15m, rsi, indicators) -> str:
    """Classify the 15m setup pattern."""
    ind = indicators or {}
    bb = ind.get("bollinger_15m") or {}
    ema_15m = ind.get("ema_stack_15m") or {}
    macd = ind.get("macd_15m") or {}
    vwap = ind.get("vwap")

    price = float(candles_15m[-1][4]) if candles_15m else None
    rsi_v = float(rsi) if rsi is not None else 50.0

    # Reversal: RSI deeply oversold + near lower Bollinger Band
    if rsi_v <= 30 and bb.get("pct_b", 100) <= 25:
        return "REVERSAL"

    # Breakout: bullish EMA stack + MACD positive
    ema_align = ema_15m.get("alignment", "MIXED")
    macd_hist = float(macd.get("histogram") or 0)
    if ema_align in _BULLISH_ALIGNMENTS and macd_hist > 0:
        return "BREAKOUT"

    # Pullback: price very close to VWAP (within 0.5%)
    if vwap and price and abs(price - vwap) / vwap < 0.005:
        return "PULLBACK"

    # Range compression squeeze
    bw = bb.get("bandwidth", 99)
    if bw is not None and bw < 2.0:
        return "CONSOLIDATION"

    return "NONE"


def _detect_candle_patterns(candles_15m) -> str:
    """Detect significant single and two-bar candle patterns in the last few 15m bars."""
    if not candles_15m or len(candles_15m) < 2:
        return "NONE"

    patterns = []
    last = candles_15m[-1]
    prev = candles_15m[-2]
    o, h, l, c = float(last[1]), float(last[2]), float(last[3]), float(last[4])
    po, ph, pl, pc = float(prev[1]), float(prev[2]), float(prev[3]), float(prev[4])

    body = abs(c - o)
    total_range = h - l
    upper_wick = h - max(c, o)
    lower_wick = min(c, o) - l

    if total_range > 0:
        # Doji: body < 10% of total range
        if body / total_range < 0.10:
            patterns.append("doji")
        # Hammer: lower wick > 2× body, upper wick < body, net bullish candle or small body
        elif lower_wick > 2 * body and upper_wick < body and c >= o:
            patterns.append("hammer at recent low")
        # Shooting star: upper wick > 2× body, lower wick < body
        elif upper_wick > 2 * body and lower_wick < body and c <= o:
            patterns.append("shooting star")

    # Bullish engulfing: previous bar bearish, current bar bullish and engulfs previous body
    if pc < po and c > o and c > po and o < pc:
        patterns.append("bullish engulfing")
    # Bearish engulfing
    elif pc > po and c < o and c < po and o > pc:
        patterns.append("bearish engulfing")

    return ", ".join(patterns) if patterns else "NONE"


def _volume_verdict(candles_15m, rvol) -> str:
    """Classify the volume-price relationship."""
    rv = float(rvol) if rvol is not None else 1.0
    if rv >= 2.0:
        return "CONFIRMING"
    if rv < 0.7:
        return "CONTRADICTING"
    return "NEUTRAL"


def _compute_sl_pct(indicators, price, strategy) -> float:
    """Compute stop-loss percent below entry: ATR-based when available, else default."""
    atr = indicators.get("atr") if indicators else None
    profile = STRATEGY_PROFILES.get(strategy, STRATEGY_PROFILES["CONSERVATIVE"])
    default_sl = profile["sl_default_pct"]

    if atr and price and price > 0:
        atr_pct = atr / price * 100
        atr_sl = round(atr_pct * 1.5, 2)
        return max(atr_sl, 1.2)

    return default_sl


def _build_confluence_conservative(
    price, rsi, rvol, indicators, ema_stack_1h
) -> tuple[list, list]:
    """5 confluence signals for CONSERVATIVE strategy."""
    ind = indicators or {}
    vwap = ind.get("vwap")
    ema_15m = ind.get("ema_stack_15m") or {}
    macd = ind.get("macd_15m") or {}
    rsi_v = float(rsi) if rsi is not None else 50.0
    rv = float(rvol) if rvol is not None else 0.0
    macd_hist = float(macd.get("histogram") or 0)
    macd_hist_prev = macd.get("histogram_prev")
    macd_growing = macd_hist_prev is not None and abs(macd_hist) > abs(float(macd_hist_prev))

    pro, con = [], []

    # 1. Price above VWAP
    if vwap and price and price > vwap:
        pro.append(f"Price above VWAP ({price:.4g} > {vwap:.4g})")
    else:
        con.append("Price below VWAP" if vwap else "VWAP unavailable")

    # 2. 15m EMA stack bullish
    ema_align = ema_15m.get("alignment", "MIXED")
    if ema_align in _BULLISH_ALIGNMENTS:
        pro.append(f"EMA stack (15m) {ema_align}")
    else:
        con.append(f"EMA stack (15m) {ema_align} — no bullish alignment")

    # 3. MACD histogram positive and growing
    if macd_hist > 0 and macd_growing:
        pro.append("MACD histogram positive and growing")
    elif macd_hist > 0:
        pro.append("MACD histogram positive (momentum not accelerating)")
    else:
        con.append("MACD histogram negative or flat")

    # 4. RSI 45–65
    if 45 <= rsi_v <= 65:
        pro.append(f"RSI {rsi_v:.1f} in optimal range 45–65")
    elif rsi_v > 65:
        con.append(f"RSI {rsi_v:.1f} elevated — overbought risk")
    else:
        con.append(f"RSI {rsi_v:.1f} below 45 — momentum weak for conservative")

    # 5. RVOL >= 2.0
    if rv >= 2.0:
        pro.append(f"RVOL {rv:.2f}x — strong volume participation")
    else:
        con.append(f"RVOL {rv:.2f}x — below 2.0 minimum")

    # Hard gate: 1h trend must be BULLISH or NEUTRAL
    trend_1h = _detect_trend_1h(None, ema_stack_1h)
    if trend_1h == "BEARISH":
        con.append("1h trend BEARISH — hard disqualifier for CONSERVATIVE")

    return pro, con


def _build_confluence_aggressive(
    price, rsi, rvol, indicators
) -> tuple[list, list]:
    """5 confluence signals for AGGRESSIVE strategy."""
    ind = indicators or {}
    vwap = ind.get("vwap")
    ema_15m = ind.get("ema_stack_15m") or {}
    ema_1h = ind.get("ema_stack_1h") or {}
    macd = ind.get("macd_15m") or {}
    rsi_v = float(rsi) if rsi is not None else 50.0
    rv = float(rvol) if rvol is not None else 0.0
    macd_hist = float(macd.get("histogram") or 0)
    macd_hist_prev = macd.get("histogram_prev")

    pro, con = [], []

    # 1. 15m EMA stack bullish (required)
    ema_align = ema_15m.get("alignment", "MIXED")
    if ema_align in _BULLISH_ALIGNMENTS:
        pro.append(f"EMA stack (15m) {ema_align} — momentum entry trend")
    else:
        con.append(f"EMA stack (15m) {ema_align} — required for AGGRESSIVE")

    # 2. RVOL >= 1.5
    if rv >= 1.5:
        pro.append(f"RVOL {rv:.2f}x — volume confirms move")
    else:
        con.append(f"RVOL {rv:.2f}x — below 1.5 minimum")

    # 3. MACD histogram positive
    if macd_hist > 0:
        macd_hist_prev_f = float(macd_hist_prev) if macd_hist_prev is not None else None
        if macd_hist_prev_f is not None and abs(macd_hist) < abs(macd_hist_prev_f):
            con.append("MACD histogram positive but shrinking — momentum fading")
        else:
            pro.append("MACD histogram positive")
    else:
        con.append("MACD histogram non-positive")

    # 4. 1h trend alignment
    ema_1h_align = ema_1h.get("alignment", "MIXED")
    if ema_1h_align in _BULLISH_ALIGNMENTS:
        pro.append(f"1h EMA stack {ema_1h_align} — ideal momentum")
    elif ema_1h_align not in _BEARISH_ALIGNMENTS:
        pro.append("1h trend neutral — acceptable for AGGRESSIVE")
    else:
        if rv >= 3.0:
            pro.append(f"1h EMA {ema_1h_align} but RVOL {rv:.1f}x — momentum override")
        else:
            con.append(f"1h EMA {ema_1h_align} and RVOL < 3.0 — requires RVOL>3 to override")

    # 5. Price above VWAP
    if vwap and price and price > vwap:
        pro.append(f"Price above VWAP ({price:.4g})")
    else:
        con.append("Price at or below VWAP")

    return pro, con


def _build_confluence_reversal(
    price, rsi, rvol, indicators, candles_15m
) -> tuple[list, list]:
    """3 exhaustion signals for REVERSAL strategy (need 2/3)."""
    ind = indicators or {}
    bb = ind.get("bollinger_15m") or {}
    macd = ind.get("macd_15m") or {}
    rsi_v = float(rsi) if rsi is not None else 50.0
    rv = float(rvol) if rvol is not None else 0.0
    macd_hist = float(macd.get("histogram") or 0)
    macd_hist_prev = macd.get("histogram_prev")

    pro, con = [], []

    # 1. RSI < 30 (hard requirement)
    if rsi_v < 30:
        pro.append(f"RSI {rsi_v:.1f} — deeply oversold exhaustion")
    else:
        con.append(f"RSI {rsi_v:.1f} — not oversold enough (<30 required)")

    # 2. Price at/near lower Bollinger Band
    pct_b = bb.get("pct_b", 100)
    if pct_b is not None and pct_b <= 25:
        pro.append(f"Price near lower BB (pct_B={pct_b:.0f}%) — support level")
    else:
        con.append(f"Price not at lower BB (pct_B={pct_b:.0f}%) — not at reversal zone")

    # 3. Seller exhaustion: MACD bearish but shrinking magnitude
    if macd_hist < 0 and macd_hist_prev is not None:
        if abs(macd_hist) < abs(float(macd_hist_prev)):
            pro.append("MACD bearish but histogram shrinking — sellers exhausting")
        else:
            con.append("MACD bearish and accelerating — sellers still in control")
    elif macd_hist >= 0:
        con.append("MACD not bearish — not a reversal setup")
    else:
        con.append("MACD histogram_prev unavailable — can't confirm exhaustion")

    # 4. RVOL >= 3.0 (capitulation spike)
    if rv >= 3.0:
        pro.append(f"RVOL {rv:.2f}x — capitulation volume spike")
    else:
        con.append(f"RVOL {rv:.2f}x — insufficient capitulation (need 3.0x)")

    # 5. Volume decreasing on recent down bars (last 3 bearish candles)
    if candles_15m and len(candles_15m) >= 4:
        recent = candles_15m[-4:]
        bearish_vols = [float(c[5]) for c in recent if float(c[4]) < float(c[1])]
        if len(bearish_vols) >= 2 and bearish_vols[-1] < bearish_vols[0]:
            pro.append("Decreasing sell volume on recent down bars — sellers fading")
        else:
            con.append("Sell volume not clearly decreasing")

    return pro, con


def _btc_bias_gate(btc_bias) -> tuple[bool, str]:
    """Return (block, reason) for hard BTC bias gate."""
    if btc_bias == "STRONG_BEARISH":
        return True, "STRONG_BEARISH BTC — avoid all altcoin longs"
    return False, ""


# ── Public API ─────────────────────────────────────────────────────────────────

def make_decision(
    symbol: str,
    price: float,
    rsi,
    rvol,
    candles_15m: list,
    candles_1h: list,
    high_24h=None,
    low_24h=None,
    indicators: dict = None,
    change_24h=None,
    volume_24h=None,
    strategy: str = "CONSERVATIVE",
    btc_bias: str = "NEUTRAL",
    regime_ctx: dict = None,
) -> tuple[dict, str]:
    """Deterministic trading decision engine.

    Returns (analysis_dict, signal_id) — identical output schema to get_ai_verdict().
    """
    signal_id = str(uuid.uuid4())
    ind = indicators or {}
    profile = STRATEGY_PROFILES.get(strategy.upper(), STRATEGY_PROFILES["CONSERVATIVE"])
    strat = strategy.upper()

    # ── Hard gate: BTC macro ────────────────────────────────────────────────────
    block, block_reason = _btc_bias_gate(btc_bias)
    if block:
        return {
            "trend_1h": "UNKNOWN",
            "setup_15m": "NONE",
            "candle_patterns": "NONE",
            "volume_verdict": "NEUTRAL",
            "confluence_signals": [],
            "conflicting_signals": [block_reason],
            "setup_grade": "C",
            "verdict": "WAIT",
            "stop_loss_pct": 0.0,
            "take_profit_pct": 0.0,
            "rr_ratio": 0.0,
            "confidence": 0,
            "reason": f"BTC macro block: {block_reason}",
        }, signal_id

    # ── Trend and setup ─────────────────────────────────────────────────────────
    ema_1h = ind.get("ema_stack_1h")
    ema_4h = ind.get("ema_stack_4h")
    trend_1h = _detect_trend_1h(candles_1h, ema_1h)
    setup_15m = _detect_setup_15m(candles_15m, rsi, ind)
    candle_patterns = _detect_candle_patterns(candles_15m)
    vol_verdict = _volume_verdict(candles_15m, rvol)

    # ── Strategy-specific confluence signals ────────────────────────────────────
    if strat == "REVERSAL":
        pro_signals, con_signals = _build_confluence_reversal(
            price, rsi, rvol, ind, candles_15m
        )
    elif strat == "AGGRESSIVE":
        pro_signals, con_signals = _build_confluence_aggressive(price, rsi, rvol, ind)
    else:  # CONSERVATIVE (default)
        pro_signals, con_signals = _build_confluence_conservative(
            price, rsi, rvol, ind, ema_1h
        )

    # ── Hard gates ──────────────────────────────────────────────────────────────
    hard_block = False
    hard_reason = None

    rsi_v = float(rsi) if rsi is not None else 50.0

    if strat == "CONSERVATIVE":
        if trend_1h == "BEARISH":
            hard_block = True
            hard_reason = "1h trend BEARISH — hard disqualifier for CONSERVATIVE"
        elif not (profile["rsi_min"] <= rsi_v <= profile["rsi_max"]):
            hard_block = True
            hard_reason = f"RSI {rsi_v:.1f} outside 45–65 range for CONSERVATIVE"

    elif strat == "AGGRESSIVE":
        ema_15m_align = (ind.get("ema_stack_15m") or {}).get("alignment", "MIXED")
        if ema_15m_align not in _BULLISH_ALIGNMENTS:
            hard_block = True
            hard_reason = "15m EMA stack not bullish — required for AGGRESSIVE"
        elif rsi_v > profile["rsi_max"]:
            hard_block = True
            hard_reason = f"RSI {rsi_v:.1f} > {profile['rsi_max']} — overbought hard cap"

    elif strat == "REVERSAL":
        if rsi_v >= 30:
            hard_block = True
            hard_reason = f"RSI {rsi_v:.1f} >= 30 — not oversold enough for REVERSAL"
        pct_b = (ind.get("bollinger_15m") or {}).get("pct_b", 100)
        if pct_b is not None and pct_b > 35:
            hard_block = True
            hard_reason = f"Price not near lower BB (pct_B={pct_b:.0f}%) — REVERSAL requires < 35%"

    # BTC BEARISH_HEADWIND: require A-grade (handled by raising confluence requirement)
    btc_headwind = (btc_bias == "BEARISH_HEADWIND")

    # ── SL / TP calculation ─────────────────────────────────────────────────────
    sl_pct = _compute_sl_pct(ind, price, strat)
    tp_pct = round(sl_pct * profile["min_rr"], 2)
    rr = round(tp_pct / sl_pct, 2) if sl_pct > 0 else 0.0

    # ── Confluence count and grade ──────────────────────────────────────────────
    min_required = profile["min_confluence"]
    if btc_headwind:
        min_required = min_required + 1  # raise bar by one under headwind

    n_pro = len(pro_signals)
    grade = "C"
    if n_pro >= min_required + 1:
        grade = "A"
    elif n_pro >= min_required:
        grade = "B" if not btc_headwind else "B"  # B still, but headwind raises bar

    # CONSERVATIVE and REVERSAL require A-grade; AGGRESSIVE accepts B-grade
    min_grade_required = "A" if strat in ("CONSERVATIVE", "REVERSAL") else "B"
    grade_ok = (grade == "A") or (grade == "B" and min_grade_required == "B")

    # ── Regime gating ──────────────────────────────────────────────────────────
    regime_block = False
    if regime_ctx:
        allowed = regime_ctx.get("active_strategies", [])
        if allowed and strat not in allowed:
            regime_block = True
            con_signals.append(f"Strategy {strat} not active in {regime_ctx.get('regime')} regime")

    # ── Final verdict ────────────────────────────────────────────────────────────
    if hard_block or regime_block:
        verdict = "WAIT"
        grade = "C"
    elif n_pro < min_required:
        verdict = "WAIT"
    elif rr < profile["min_rr"]:
        verdict = "WAIT"
        con_signals.append(f"R:R {rr:.2f} below minimum {profile['min_rr']}")
    elif not grade_ok:
        verdict = "WAIT"
    else:
        verdict = "BUY"

    # Confidence: (n_pro / 6) * 100, capped at 95
    confidence = min(95, int(n_pro / 6 * 100))

    # ── Reason ──────────────────────────────────────────────────────────────────
    if verdict == "BUY":
        reason = (
            f"{trend_1h} 1h trend, {setup_15m} setup on 15m. "
            f"{n_pro} confluence signals align: {'; '.join(pro_signals[:2])}. "
            f"SL {sl_pct:.2f}% structural, TP {tp_pct:.2f}% at resistance, R:R {rr:.2f}."
        )
    else:
        top_con = con_signals[0] if con_signals else "Insufficient confluence"
        reason = (
            f"WAIT — {strat}: {top_con}. "
            f"Only {n_pro}/{min_required} required signals confirmed. "
            f"Grade {grade} ({min_grade_required} required)."
        )
        if hard_reason:
            reason = f"WAIT — Hard gate: {hard_reason}."

    return {
        "trend_1h": trend_1h,
        "setup_15m": setup_15m,
        "candle_patterns": candle_patterns,
        "volume_verdict": vol_verdict,
        "confluence_signals": pro_signals,
        "conflicting_signals": con_signals,
        "setup_grade": grade,
        "verdict": verdict,
        "stop_loss_pct": sl_pct if verdict == "BUY" else 0.0,
        "take_profit_pct": tp_pct if verdict == "BUY" else 0.0,
        "rr_ratio": rr if verdict == "BUY" else 0.0,
        "confidence": confidence,
        "reason": reason,
    }, signal_id
