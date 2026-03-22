import json
import os
import sys
import time
from datetime import datetime, timezone

import redis
from dotenv import load_dotenv

load_dotenv()

# Allow importing shared (project root or /app in Docker)
_this_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.abspath(os.path.join(_this_dir, "..", "..")) if os.path.basename(_this_dir) == "filter" else _this_dir
if _root not in sys.path:
    sys.path.insert(0, _root)

from shared import config as shared_config
from shared import db as shared_db
from shared import logger as shared_logger

log = shared_logger.get_logger("filter")

# Hardcoded strategy profiles: min_24h_volume, rvol_threshold, rsi_max, min_change (%)
# REVERSAL: rsi_max used as oversold threshold (RSI < 30), min_change negative (price drop)
STRATEGY_PROFILES = {
    "CONSERVATIVE": {
        "min_24h_volume": 10_000_000,
        "rvol_threshold": 1.5,   # current bar >= 1.5× 20-bar avg
        "rsi_min": 40,           # exclude deeply oversold (counter-trend) in momentum mode
        "rsi_max": 70,
        "rsi_1h_max": 70,        # 1h not overbought — avoids entering late into extended moves
        "min_change": 1.0,       # min price change over last 4h (16 × 15m bars); captures fresh moves
    },
    "AGGRESSIVE": {
        "min_24h_volume": 5_000_000,
        "rvol_threshold": 1.2,   # current bar >= 1.2× 20-bar avg
        "rsi_min": 35,           # wider range for momentum breakouts
        "rsi_max": 85,
        "rsi_1h_max": 80,        # lenient 1h check — accepts overbought-but-running momentum
        "min_change": 2.0,       # stronger recent move required for aggressive breakout entries
    },
    "REVERSAL": {
        "min_24h_volume": 20_000_000,
        "rvol_threshold": 3.0,   # capitulation spike required
        "rsi_min": 0,            # no lower bound — oversold is the goal
        "rsi_max": 30,           # 15m oversold
        "rsi_1h_max": 40,        # 1h must also confirm oversold (macro weakness, not just a 15m blip)
        "min_change": -2.5,      # min drop over last 4h; -2.5% + RSI<30 + RVOL>3 = genuine panic
    },
}
STRATEGY_DEFAULT = "CONSERVATIVE"


class Filter:
    def __init__(self):
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        self.rsi_period = 14

    def calculate_rsi(self, candles):
        """Compute RSI using Wilder's smoothing — matches TradingView exactly.

        Seeds avg_gain/avg_loss with the SMA of the first `rsi_period` moves,
        then applies: avg = (prev_avg * (period-1) + current) / period.
        Returns 50 (neutral) when there is insufficient data.
        """
        period = self.rsi_period
        if len(candles) < period + 1:
            return 50

        closes = [c[4] for c in candles]
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

        gains = [d if d > 0 else 0.0 for d in deltas]
        losses = [-d if d < 0 else 0.0 for d in deltas]

        # Seed with SMA of first `period` moves
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        # Wilder's smoothing over remaining moves
        for gain, loss in zip(gains[period:], losses[period:]):
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)

    def _compute_rvol_from_candles(self, candles: list, period: int = 50) -> float:
        """Standard RVOL: current bar volume / average volume of prior `period` bars.

        This matches how RVOL is displayed on TradingView and professional platforms.
        Values > 1.0 = above average. > 2.0 = strong. > 3.0 = exceptional.
        Uses the most recent 15m candle as 'current' and the preceding `period` candles
        as the baseline. Returns 0.0 when there is insufficient data.
        """
        if len(candles) < period + 2:
            return 0.0
        # Use candles[-2] (last CLOSED bar) — candles[-1] is the currently-forming bar
        # whose volume is only a fraction of its eventual total, causing false negatives.
        current_vol = candles[-2][5]
        avg_vol = sum(c[5] for c in candles[-(period + 2):-2]) / period
        return round(current_vol / avg_vol, 2) if avg_vol > 0 else 0.0

    # ------------------------------------------------------------------
    # Technical indicator computation (pure functions — no I/O)
    # ------------------------------------------------------------------

    def _compute_ema(self, prices: list, period: int) -> list:
        """Compute Exponential Moving Average seeded with SMA.

        Returns a list of EMA values starting from index `period-1` of the
        input series. Returns [] if there is insufficient data.
        """
        if len(prices) < period:
            return []
        k = 2.0 / (period + 1)
        # Seed first EMA value as the simple average of the first `period` prices
        ema_values = [sum(prices[:period]) / period]
        for price in prices[period:]:
            ema_values.append(price * k + ema_values[-1] * (1 - k))
        return ema_values

    def _compute_vwap(self, candles: list) -> float | None:
        """Compute Volume-Weighted Average Price over all provided candles.

        Uses typical price = (high + low + close) / 3.
        Returns None when candles are empty or total volume is zero.
        """
        if not candles:
            return None
        total_tv = sum((c[2] + c[3] + c[4]) / 3.0 * c[5] for c in candles)
        total_vol = sum(c[5] for c in candles)
        return total_tv / total_vol if total_vol > 0 else None

    def _compute_atr(self, candles: list, period: int = 14) -> float | None:
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
        # Seed ATR with SMA of the first `period` true ranges
        atr = sum(true_ranges[:period]) / period
        # Apply Wilder's smoothing for remaining true ranges
        for tr in true_ranges[period:]:
            atr = (atr * (period - 1) + tr) / period
        return atr

    def _compute_bollinger_bands(
        self, closes: list, period: int = 20, num_std: float = 2.0
    ) -> dict | None:
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

    def _compute_macd(
        self, closes: list, fast: int = 12, slow: int = 26, signal: int = 9
    ) -> dict | None:
        """Compute MACD line, signal line, and histogram.

        Returns dict with: macd, signal_line, histogram, histogram_prev.
        histogram_prev enables momentum direction detection (growing vs. shrinking).
        Returns None when there is insufficient data (need >= slow + signal prices).
        """
        if len(closes) < slow + signal:
            return None
        ema_fast = self._compute_ema(closes, fast)
        ema_slow = self._compute_ema(closes, slow)
        if not ema_fast or not ema_slow:
            return None
        # Align: both series end at the same price point; ema_fast is longer by (slow-fast)
        offset = len(ema_fast) - len(ema_slow)
        macd_line = [ema_fast[i + offset] - ema_slow[i] for i in range(len(ema_slow))]
        signal_ema = self._compute_ema(macd_line, signal)
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

    def _compute_ema_stack(self, closes: list) -> dict | None:
        """Compute EMA 9/21/50 and describe their alignment as a trend label.

        Returns dict with: ema9, ema21, ema50, alignment, description.
        Returns None when there is insufficient data for EMA 50.
        """
        ema9 = self._compute_ema(closes, 9)
        ema21 = self._compute_ema(closes, 21)
        ema50 = self._compute_ema(closes, 50)
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

    def _compute_adx(self, candles: list, period: int = 14) -> float | None:
        """Compute ADX (Average Directional Index) using Wilder's smoothing.

        ADX measures trend *strength*, not direction: >25 = trending, <20 = ranging.
        Uses Wilder's method: smooth +DM, -DM, ATR separately; compute DX at each bar;
        then smooth DX values into ADX.
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
        # Seed smoothed values with sum of first `period` bars
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

    def _compute_recent_change(self, candles_15m: list, lookback: int = 16) -> float:
        """Price change over the last `lookback` 15m bars (default 16 = 4 hours).

        Uses closed candles: candles[-2] (last closed) vs candles[-(lookback+2)] (start).
        This captures fresh momentum — unlike 24h change which reflects yesterday's moves.
        Returns 0.0 when there are insufficient candles.
        """
        needed = lookback + 2  # lookback bars + 1 closed current + 1 forming
        if len(candles_15m) < needed:
            return 0.0
        price_now = candles_15m[-2][4]
        price_then = candles_15m[-(lookback + 2)][4]
        if price_then == 0:
            return 0.0
        return round((price_now - price_then) / price_then * 100, 2)

    def _score_candidate(self, candidate: dict, strategy_name: str) -> float:
        """Score a candidate 0–100. Brain processes highest-scored candidates first.

        Dimensions:
          RVOL (40 pts) — volume participation, capped at 4× to avoid outliers dominating
          EMA alignment (30 pts) — trend quality
          RSI quality (20 pts) — momentum sweet spot vs overbought/oversold
          MACD histogram (10 pts) — momentum direction and strength
        """
        score = 0.0

        # RVOL: 0–40 pts, capped at 4×
        rvol = candidate.get('rvol') or 0
        score += min(rvol / 4.0, 1.0) * 40

        # EMA alignment quality
        ema_alignment = (candidate.get('ema_stack_15m') or {}).get('alignment', 'MIXED')
        ema_pts = {'BULLISH': 30, 'RECOVERING': 20, 'MIXED': 10, 'WEAKENING': 5, 'BEARISH': 0}
        score += ema_pts.get(ema_alignment, 10)

        # RSI quality: reversal wants deeply oversold; momentum wants centred 50–60
        rsi = candidate.get('rsi') or 50
        if strategy_name == 'REVERSAL':
            score += max(0.0, (30 - rsi) / 30 * 20)
        else:
            score += max(0.0, (1.0 - abs(rsi - 55) / 45) * 20)

        # MACD histogram: positive and growing = full 10 pts; positive but flat = 5 pts
        macd = candidate.get('macd_15m') or {}
        hist = macd.get('histogram') or 0
        hist_prev = macd.get('histogram_prev') or 0
        if hist > 0 and hist > hist_prev:
            score += 10
        elif hist > 0:
            score += 5

        return round(score, 1)

    def _compute_technical_indicators(
        self, candles_15m: list, candles_1h: list, candles_4h: list | None = None
    ) -> dict:
        """Orchestrate all technical indicator computations.

        Returns a flat dict of indicator values ready to merge into the candidate payload.
        All fields are None when candles are missing or insufficient.
        """
        result = {}

        if candles_15m:
            closes_15m = [c[4] for c in candles_15m]
            # Session VWAP: filter candles to today 00:00 UTC (institutional daily reset).
            # Falls back to last 32 candles early in the session (< 10 today candles).
            today_midnight_ms = int(
                datetime.now(timezone.utc)
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .timestamp() * 1000
            )
            session_candles = [c for c in candles_15m if c[0] >= today_midnight_ms]
            if len(session_candles) < 10:
                session_candles = candles_15m[-32:]
            result["vwap"] = self._compute_vwap(session_candles)
            result["atr"] = self._compute_atr(candles_15m, period=14)
            result["ema_stack_15m"] = self._compute_ema_stack(closes_15m)
            result["bollinger_15m"] = self._compute_bollinger_bands(closes_15m)
            result["macd_15m"] = self._compute_macd(closes_15m)
        else:
            result.update({"vwap": None, "atr": None, "ema_stack_15m": None,
                           "bollinger_15m": None, "macd_15m": None})

        # 1h EMA stack gives higher-timeframe trend bias
        if candles_1h:
            closes_1h = [c[4] for c in candles_1h]
            result["ema_stack_1h"] = self._compute_ema_stack(closes_1h)
        else:
            result["ema_stack_1h"] = None

        # 4h EMA stack: mandatory trend gate (dominant direction over days, not hours)
        if candles_4h:
            closes_4h = [c[4] for c in candles_4h]
            result["ema_stack_4h"] = self._compute_ema_stack(closes_4h)
        else:
            result["ema_stack_4h"] = None

        return result

    def _compute_and_store_btc_context(self) -> None:
        """Compute BTC/USDT market indicators and publish to Redis as btc_context.

        Reads market_data:BTC/USDT directly so this can run as soon as Scout
        writes BTC data — independently of whether the full active_symbols list
        is available yet. Brain reads btc_context for macro bias on every signal.
        """
        btc_raw = self.db.get("market_data:BTC/USDT")

        if not btc_raw:
            log.warning("Filter: market_data:BTC/USDT not in Redis — Scout hasn't written BTC data yet")
            return

        try:
            data = json.loads(btc_raw)
        except json.JSONDecodeError as e:
            log.warning(f"Filter: market_data:BTC/USDT JSON decode error: {e}")
            return

        candles_15m = data.get('candles_15m', [])
        candles_1h = data.get('candles_1h', [])
        candles_4h = data.get('candles_4h', [])
        if not candles_15m:
            log.warning("Filter: market_data:BTC/USDT has no candles_15m — cannot compute BTC context")
            return

        rsi = self.calculate_rsi(candles_15m)
        indicators = self._compute_technical_indicators(candles_15m, candles_1h)

        # 4h EMA, ADX, and volatility ratio — regime detection inputs
        ema_stack_4h = None
        adx_4h = None
        atr_ratio = None
        if candles_4h:
            closes_4h = [c[4] for c in candles_4h]
            ema_stack_4h = self._compute_ema_stack(closes_4h)
            adx_4h = self._compute_adx(candles_4h, period=14)
            # Volatility ratio: RMS of recent 14-bar returns vs 50-bar historical
            if len(candles_4h) >= 65:
                recent_ret = [(candles_4h[i][4] - candles_4h[i - 1][4]) / candles_4h[i - 1][4]
                              for i in range(len(candles_4h) - 14, len(candles_4h))]
                hist_ret = [(candles_4h[i][4] - candles_4h[i - 1][4]) / candles_4h[i - 1][4]
                            for i in range(len(candles_4h) - 50, len(candles_4h))]
                vol_r = (sum(r * r for r in recent_ret) / len(recent_ret)) ** 0.5
                vol_h = (sum(r * r for r in hist_ret) / len(hist_ret)) ** 0.5
                atr_ratio = round(vol_r / vol_h, 2) if vol_h > 0 else 1.0

        price = data.get('last_price')
        vwap = indicators.get('vwap')
        vwap_pct = None
        if price and vwap:
            vwap_pct = round((float(price) - vwap) / vwap * 100, 2)

        btc_context = {
            "price": price,
            "change_24h": data.get('change_24h'),
            "rsi": rsi,
            "vwap_pct": vwap_pct,
            "ema_stack_15m": indicators.get('ema_stack_15m'),
            "ema_stack_1h":  indicators.get('ema_stack_1h'),
            "macd_15m":      indicators.get('macd_15m'),
            "ema_stack_4h":  ema_stack_4h,
            "adx_4h":        adx_4h,
            "atr_ratio":     atr_ratio,
        }

        # TTL 180s — valid across multiple filter cycles (filter runs every 10s)
        self.db.set(shared_config.REDIS_KEY_BTC_CONTEXT, json.dumps(btc_context), ex=180)
        vwap_str = f"{vwap_pct:+.2f}%" if vwap_pct is not None else "N/A"
        log.info(
            f"Filter: BTC context updated — RSI: {rsi} "
            f"| EMA(15m): {(indicators.get('ema_stack_15m') or {}).get('alignment', 'N/A')} "
            f"| EMA(1h): {(indicators.get('ema_stack_1h') or {}).get('alignment', 'N/A')} "
            f"| EMA(4h): {(ema_stack_4h or {}).get('alignment', 'N/A')} "
            f"| ADX(4h): {adx_4h or 'N/A'} "
            f"| VWAP: {vwap_str}"
        )

    def _compute_and_store_market_regime(self, breadth_stats: dict) -> None:
        """Classify market regime from BTC 4h trend + ADX + market breadth + volatility.

        Four regimes — each drives different strategy gating and position sizing:
          BULL_TRENDING  — momentum strategies favored, full sizing
          BEAR_TRENDING  — only reversal setups pass, half sizing
          RANGING        — mean-reversion favored, breakouts disabled, 75% sizing
          MIXED          — uncertain, conservative strategies only, 75% sizing

        Volatility overlay (ELEVATED/EXTREME) further reduces sizing on top of regime.
        Publishes to Redis with 5-minute TTL so downstream services stay fresh.
        """
        btc_raw = self.db.get(shared_config.REDIS_KEY_BTC_CONTEXT)
        if not btc_raw:
            return
        try:
            btc = json.loads(btc_raw)
        except Exception:
            return

        btc_4h_align = (btc.get('ema_stack_4h') or {}).get('alignment', 'MIXED')
        adx_4h = btc.get('adx_4h') or 0.0
        btc_rsi = float(btc.get('rsi') or 50)
        atr_ratio = float(btc.get('atr_ratio') or 1.0)

        total = breadth_stats.get('total', 0)
        breadth_bull_pct = round(breadth_stats['bullish_4h'] / total * 100, 1) if total > 0 else 50.0
        breadth_rsi_pct  = round(breadth_stats['rsi_above_50'] / total * 100, 1) if total > 0 else 50.0

        # Volatility regime from 4h realized-vol ratio
        if atr_ratio > 2.0:
            vol_regime = "EXTREME"
        elif atr_ratio > 1.5:
            vol_regime = "ELEVATED"
        else:
            vol_regime = "NORMAL"

        # Confluence scoring — each signal votes bull or bear (max 5 each side)
        bull_votes = bear_votes = 0
        if btc_4h_align in ('BULLISH', 'RECOVERING'):   bull_votes += 1
        if btc_4h_align in ('BEARISH', 'WEAKENING'):    bear_votes += 1
        if breadth_bull_pct >= 55:                       bull_votes += 1
        if breadth_bull_pct <= 40:                       bear_votes += 1
        if adx_4h >= 25 and btc_4h_align in ('BULLISH', 'RECOVERING'):  bull_votes += 1
        if adx_4h >= 25 and btc_4h_align in ('BEARISH', 'WEAKENING'):   bear_votes += 1
        if btc_rsi >= 50:                                bull_votes += 1
        if btc_rsi < 50:                                 bear_votes += 1
        if breadth_rsi_pct >= 55:                        bull_votes += 1
        if breadth_rsi_pct < 45:                         bear_votes += 1

        # Regime classification
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

        # Strategy gating per regime
        if regime == "BULL_TRENDING":
            active_strategies = ["CONSERVATIVE", "AGGRESSIVE", "REVERSAL"]
            size_mult = 1.0
        elif regime == "BEAR_TRENDING":
            active_strategies = ["REVERSAL"]   # only counter-trend longs in a downtrend
            size_mult = 0.5
        elif regime == "RANGING":
            active_strategies = ["CONSERVATIVE", "REVERSAL"]  # breakouts fail in ranges
            size_mult = 0.75
        else:  # MIXED
            active_strategies = ["CONSERVATIVE", "REVERSAL"]
            size_mult = 0.75

        # Volatility overlay reduces sizing further on top of regime
        if vol_regime == "EXTREME":
            size_mult = round(size_mult * 0.5, 2)
        elif vol_regime == "ELEVATED":
            size_mult = round(size_mult * 0.75, 2)

        payload = {
            "regime": regime,
            "confidence": confidence,
            "btc_4h_alignment": btc_4h_align,
            "adx_4h": round(adx_4h, 2) if adx_4h else None,
            "breadth_bullish_pct": breadth_bull_pct,
            "breadth_rsi_above_50_pct": breadth_rsi_pct,
            "vol_regime": vol_regime,
            "atr_ratio": round(atr_ratio, 2),
            "active_strategies": active_strategies,
            "position_size_multiplier": size_mult,
            "updated_at": datetime.now(timezone.utc).isoformat() + "Z",
        }
        self.db.set(shared_config.REDIS_KEY_MARKET_REGIME, json.dumps(payload), ex=300)
        log.info(
            f"Filter: Market regime → {regime} ({confidence}% confidence) "
            f"| Breadth: {breadth_bull_pct:.0f}% bullish | ADX: {adx_4h:.1f} "
            f"| Vol: {vol_regime} ({atr_ratio:.2f}×) | Active: {active_strategies}"
        )

    def _get_strategy(self):
        """Return current strategy name and profile. Default CONSERVATIVE."""
        val = shared_db.get_setting_value(shared_config.SYSTEM_KEY_STRATEGY)
        name = (val or STRATEGY_DEFAULT).strip().upper()
        if name not in STRATEGY_PROFILES:
            name = STRATEGY_DEFAULT
        return name, STRATEGY_PROFILES[name]

    def run(self):
        log.info("Filter: Analyzing Volume & RSI indicators...")
        PAUSED_KEY = shared_config.SYSTEM_KEY_TRADING_PAUSED

        while True:
            # Refresh BTC context unconditionally — runs regardless of paused/idle state
            # so /status always has BTC data as soon as Scout writes market_data:BTC/USDT.
            try:
                self._compute_and_store_btc_context()
            except Exception as e:
                log.warning(f"Filter: BTC context update failed: {e}")

            if shared_db.get_setting_value(PAUSED_KEY) == "1":
                time.sleep(5)
                continue

            # Stop filtering when at max open orders (no new orders would be placed)
            open_count = shared_db.get_open_order_count()
            max_open = shared_db.get_max_open_orders()
            if open_count >= max_open:
                log.info(f"Filter: Idle (max open orders reached: {open_count}/{max_open})")
                time.sleep(10)
                continue

            strategy_name, profile = self._get_strategy()
            log.info(f"Filter: Scan cycle — strategy: {strategy_name}")

            # New data layout:
            # - system:active_symbols -> JSON list of symbols
            # - market_data:{symbol}  -> JSON per-symbol payload from Scout
            raw_symbols = self.db.get(shared_config.REDIS_KEY_ACTIVE_SYMBOLS)
            if not raw_symbols:
                time.sleep(5)
                continue

            try:
                symbols = json.loads(raw_symbols)
            except json.JSONDecodeError:
                time.sleep(5)
                continue

            if not isinstance(symbols, list) or not symbols:
                time.sleep(5)
                continue

            # Fetch all per-symbol market data in one pipeline for performance
            keys = list(symbols)
            pipe = self.db.pipeline()
            for symbol in keys:
                pipe.get(f"market_data:{symbol}")
            results = pipe.execute()

            # BTC context was already refreshed at the top of the loop

            # Market breadth: scan ALL symbols for 4h EMA alignment (pre-filter universe)
            breadth_stats = {'total': 0, 'bullish_4h': 0, 'bearish_4h': 0, 'rsi_above_50': 0}
            for raw_b in results:
                if not raw_b:
                    continue
                try:
                    d_b = json.loads(raw_b)
                except Exception:
                    continue
                breadth_stats['total'] += 1
                c4h_b = d_b.get('candles_4h', [])
                if len(c4h_b) >= 50:
                    ema_b = self._compute_ema_stack([c[4] for c in c4h_b])
                    if ema_b:
                        if ema_b['alignment'] in ('BULLISH', 'RECOVERING'):
                            breadth_stats['bullish_4h'] += 1
                        elif ema_b['alignment'] in ('BEARISH', 'WEAKENING'):
                            breadth_stats['bearish_4h'] += 1
                if self.calculate_rsi(d_b.get('candles_15m', [])) > 50:
                    breadth_stats['rsi_above_50'] += 1

            # Read regime computed last cycle — used to gate candidates this cycle
            current_regime = None
            try:
                regime_raw = self.db.get(shared_config.REDIS_KEY_MARKET_REGIME)
                if regime_raw:
                    current_regime = json.loads(regime_raw)
            except Exception:
                pass

            filtered_candidates = []

            for symbol, raw_data in zip(keys, results):
                if not raw_data:
                    continue
                try:
                    data = json.loads(raw_data)
                except json.JSONDecodeError:
                    continue

                # 1. Volume Check
                if data.get('volume_24h') is None or data['volume_24h'] < profile['min_24h_volume']:
                    continue

                # 2. RVOL: last closed 15m bar vs average of prior 20 closed bars
                rvol = self._compute_rvol_from_candles(data.get('candles_15m', []))

                # 3. RSI on 15m and 1h (Wilder's smoothing)
                rsi = self.calculate_rsi(data.get('candles_15m', []))
                rsi_1h = self.calculate_rsi(data.get('candles_1h', []))

                # 4. Recent momentum: price change over last 4h (16 × 15m closed bars).
                # More responsive than 24h change — catches fresh breakouts, ignores stale moves.
                recent_change = self._compute_recent_change(data.get('candles_15m', []))

                # 5. Strategy-specific checks
                rvol_ok = rvol >= profile['rvol_threshold']
                rsi_1h_ok = rsi_1h <= profile['rsi_1h_max']
                if strategy_name == "REVERSAL":
                    rsi_ok = rsi <= profile['rsi_max']
                    change_ok = recent_change <= profile['min_change']
                else:
                    # Momentum range: not overbought AND not deeply oversold (counter-trend)
                    rsi_ok = profile['rsi_min'] <= rsi <= profile['rsi_max']
                    change_ok = recent_change >= profile['min_change']

                if rvol_ok and rsi_ok and rsi_1h_ok and change_ok:
                    # Skip if we already have an open order for this symbol
                    try:
                        with shared_db.get_connection() as conn:
                            shared_db.init_schema(conn)
                            if shared_db.get_open_order_id_for_symbol(conn, symbol) is not None:
                                continue
                    except Exception:
                        pass

                    # Compute all technical indicators for this candidate
                    indicators = self._compute_technical_indicators(
                        data.get('candles_15m', []),
                        data.get('candles_1h', []),
                        data.get('candles_4h', []),
                    )

                    # Pre-filter: skip full bearish EMA stack for momentum strategies.
                    # REVERSAL is exempt — it expects bearish structure by design.
                    ema_15m = indicators.get('ema_stack_15m') or {}
                    if strategy_name != "REVERSAL" and ema_15m.get('alignment') == "BEARISH":
                        log.debug(f"Filter: {symbol} skipped — bearish 15m EMA stack in {strategy_name} mode")
                        continue

                    # 4h EMA mandatory trend gate: reject entries against the dominant 4h trend.
                    # CONSERVATIVE: rejects BEARISH + WEAKENING (only allow neutral/bullish structures)
                    # AGGRESSIVE: rejects BEARISH only (allow weakening if 15m momentum is strong)
                    # REVERSAL: exempt (counter-trend by design — expects bearish 4h structure)
                    ema_4h = indicators.get('ema_stack_4h') or {}
                    ema_4h_align = ema_4h.get('alignment')
                    if ema_4h_align:
                        if strategy_name == "CONSERVATIVE" and ema_4h_align in ("BEARISH", "WEAKENING"):
                            log.debug(f"Filter: {symbol} skipped — 4h EMA {ema_4h_align} in CONSERVATIVE mode")
                            continue
                        elif strategy_name == "AGGRESSIVE" and ema_4h_align == "BEARISH":
                            log.debug(f"Filter: {symbol} skipped — 4h EMA BEARISH in AGGRESSIVE mode")
                            continue

                    # Market regime strategy gate: block strategies not active in current regime
                    if current_regime:
                        allowed = current_regime.get('active_strategies', [])
                        if allowed and strategy_name not in allowed:
                            log.debug(
                                f"Filter: {symbol} skipped — {strategy_name} not active "
                                f"in {current_regime['regime']} regime"
                            )
                            continue

                    data['symbol'] = symbol
                    data['rvol'] = round(rvol, 2)
                    data['rsi'] = rsi
                    data['rsi_1h'] = rsi_1h
                    data['recent_change'] = recent_change
                    data.update(indicators)
                    data['filter_score'] = self._score_candidate(data, strategy_name)

                    log.info(
                        f"Filter Match: {symbol} | Score: {data['filter_score']} "
                        f"| RVOL: {rvol:.2f} | RSI(15m): {rsi} | RSI(1h): {rsi_1h} "
                        f"| 4h Chg: {recent_change:+.2f}% "
                        f"| EMA(15m): {ema_15m.get('alignment', 'N/A')} "
                        f"| EMA(1h): {(indicators.get('ema_stack_1h') or {}).get('alignment', 'N/A')} "
                        f"| EMA(4h): {ema_4h_align or 'N/A'}"
                    )
                    filtered_candidates.append(data)

            if filtered_candidates:
                # Sort strongest signal first so Brain always analyses the best setup
                # when max_open_orders is hit mid-batch
                filtered_candidates.sort(key=lambda c: c.get('filter_score', 0), reverse=True)
                # Re-check before writing (avoid race: 10th order opened during this loop)
                if shared_db.get_open_order_count() >= shared_db.get_max_open_orders():
                    pass  # Don't write; next cycle Filter will stay idle
                else:
                    self.db.set('filtered_candidates', json.dumps(filtered_candidates))

            # Compute and publish market regime for next cycle
            try:
                self._compute_and_store_market_regime(breadth_stats)
            except Exception as e:
                log.warning(f"Filter: Market regime update failed: {e}")

            time.sleep(10)


if __name__ == "__main__":
    f = Filter()
    f.run()
