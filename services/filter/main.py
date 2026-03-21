import json
import os
import sys
import time

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

    def _compute_rvol_from_candles(self, candles: list, period: int = 20) -> float:
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

    def _compute_technical_indicators(self, candles_15m: list, candles_1h: list) -> dict:
        """Orchestrate all technical indicator computations.

        Returns a flat dict of indicator values ready to merge into the candidate payload.
        All fields are None when candles are missing or insufficient.
        """
        result = {}

        if candles_15m:
            closes_15m = [c[4] for c in candles_15m]
            result["vwap"] = self._compute_vwap(candles_15m)
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

        return result

    def _compute_and_store_btc_context(self) -> None:
        """Compute BTC/USDT market indicators and publish to Redis as btc_context.

        Reads market_data:BTC/USDT directly so this can run as soon as Scout
        writes BTC data — independently of whether the full active_symbols list
        is available yet. Brain reads btc_context for macro bias on every signal.
        """
        btc_raw = self.db.get("market_data:BTC/USDT")

        if not btc_raw:
            log.debug("Filter: market_data:BTC/USDT not in Redis — btc_context not updated")
            return

        try:
            data = json.loads(btc_raw)
        except json.JSONDecodeError:
            return

        candles_15m = data.get('candles_15m', [])
        candles_1h = data.get('candles_1h', [])
        if not candles_15m:
            return

        rsi = self.calculate_rsi(candles_15m)
        indicators = self._compute_technical_indicators(candles_15m, candles_1h)

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
        }

        # TTL 180s — valid across multiple filter cycles (filter runs every 10s)
        self.db.set(shared_config.REDIS_KEY_BTC_CONTEXT, json.dumps(btc_context), ex=180)
        vwap_str = f"{vwap_pct:+.2f}%" if vwap_pct is not None else "N/A"
        log.info(
            f"Filter: BTC context updated — RSI: {rsi} "
            f"| EMA(15m): {(indicators.get('ema_stack_15m') or {}).get('alignment', 'N/A')} "
            f"| EMA(1h): {(indicators.get('ema_stack_1h') or {}).get('alignment', 'N/A')} "
            f"| VWAP: {vwap_str}"
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
            if shared_db.get_setting_value(PAUSED_KEY) == "1":
                time.sleep(5)
                continue

            # Refresh BTC context unconditionally — runs even while Scout is still
            # doing its first scan so /status shows BTC data as soon as BTC/USDT
            # market data is written (typically within the first few seconds of Scout startup).
            self._compute_and_store_btc_context()

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
                    )

                    # Pre-filter: skip full bearish EMA stack for momentum strategies.
                    # REVERSAL is exempt — it expects bearish structure by design.
                    ema_15m = indicators.get('ema_stack_15m') or {}
                    if strategy_name != "REVERSAL" and ema_15m.get('alignment') == "BEARISH":
                        log.debug(f"Filter: {symbol} skipped — bearish 15m EMA stack in {strategy_name} mode")
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
                        f"| EMA(1h): {(indicators.get('ema_stack_1h') or {}).get('alignment', 'N/A')}"
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

            time.sleep(10)


if __name__ == "__main__":
    f = Filter()
    f.run()
