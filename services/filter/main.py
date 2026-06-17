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
from shared import indicators as ind_lib
from shared import portfolio as portfolio_lib

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
        "min_change": 0.3,       # min price change over last 4h — was 1.0%, too strict in ranging/sideways
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

    # ------------------------------------------------------------------
    # Indicator method delegates — all logic lives in shared/indicators.py
    # ------------------------------------------------------------------

    def calculate_rsi(self, candles, period=14):
        return ind_lib.compute_rsi(candles, period)

    def _compute_rvol_from_candles(self, candles, period=50):
        return ind_lib.compute_rvol(candles, period)

    def _compute_ema(self, prices, period):
        return ind_lib.compute_ema(prices, period)

    def _compute_vwap(self, candles):
        return ind_lib.compute_vwap(candles)

    def _compute_atr(self, candles, period=14):
        return ind_lib.compute_atr(candles, period)

    def _compute_bollinger_bands(self, closes, period=20, num_std=2.0):
        return ind_lib.compute_bollinger_bands(closes, period, num_std)

    def _compute_macd(self, closes, fast=12, slow=26, signal=9):
        return ind_lib.compute_macd(closes, fast, slow, signal)

    def _compute_ema_stack(self, closes):
        return ind_lib.compute_ema_stack(closes)

    def _compute_adx(self, candles, period=14):
        return ind_lib.compute_adx(candles, period)

    def _compute_recent_change(self, candles_15m, lookback=16):
        return ind_lib.compute_recent_change(candles_15m, lookback)

    def _score_candidate(self, candidate, strategy_name):
        return ind_lib.score_candidate(candidate, strategy_name)

    def _compute_technical_indicators(self, candles_15m, candles_1h, candles_4h=None):
        return ind_lib.compute_all_indicators(candles_15m, candles_1h, candles_4h)

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
            # REVERSAL for panic-selling setups; CONSERVATIVE allowed for relative-strength
            # symbols (4h EMA gate below still blocks symbols trading against their own trend)
            active_strategies = ["CONSERVATIVE", "REVERSAL"]
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

            # Get current strategy from settings (for logging/display)
            primary_strategy, _ = self._get_strategy()

            # Determine which strategies to run based on regime
            current_regime = None
            try:
                regime_raw = self.db.get(shared_config.REDIS_KEY_MARKET_REGIME)
                if regime_raw:
                    current_regime = json.loads(regime_raw)
            except Exception:
                pass

            allowed_by_regime = (
                current_regime.get('active_strategies', list(STRATEGY_PROFILES.keys()))
                if current_regime else list(STRATEGY_PROFILES.keys())
            )
            strategies_to_run = [s for s in ["CONSERVATIVE", "AGGRESSIVE", "REVERSAL"] if s in allowed_by_regime]
            log.info(f"Filter: Scan cycle — primary: {primary_strategy} | scanning: {strategies_to_run}")

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

            # Pre-fetch open orders once for correlation guard checks
            open_orders_for_corr = []
            try:
                with shared_db.get_connection() as conn:
                    shared_db.init_schema(conn)
                    open_orders_for_corr = shared_db.get_open_orders(conn)
            except Exception as e:
                log.warning(f"Filter: Could not fetch open orders for correlation guard: {e}")

            filtered_candidates = []

            for symbol, raw_data in zip(keys, results):
                if not raw_data:
                    continue
                try:
                    data = json.loads(raw_data)
                except json.JSONDecodeError:
                    continue

                # Compute shared per-symbol values once (used by all strategies)
                rvol = self._compute_rvol_from_candles(data.get('candles_15m', []))
                rsi = self.calculate_rsi(data.get('candles_15m', []))
                rsi_1h = self.calculate_rsi(data.get('candles_1h', []))
                recent_change = self._compute_recent_change(data.get('candles_15m', []))

                # Check if we already have an open order for this symbol (skip all strategies)
                try:
                    with shared_db.get_connection() as conn:
                        shared_db.init_schema(conn)
                        if shared_db.get_open_order_id_for_symbol(conn, symbol) is not None:
                            continue
                except Exception:
                    pass

                # Compute indicators once per symbol (shared across strategies)
                indicators = self._compute_technical_indicators(
                    data.get('candles_15m', []),
                    data.get('candles_1h', []),
                    data.get('candles_4h', []),
                )
                ema_15m = indicators.get('ema_stack_15m') or {}
                ema_4h = indicators.get('ema_stack_4h') or {}
                ema_4h_align = ema_4h.get('alignment')

                # Try each strategy for this symbol
                for strategy_name in strategies_to_run:
                    profile = STRATEGY_PROFILES[strategy_name]

                    # 1. Volume check
                    if data.get('volume_24h') is None or data['volume_24h'] < profile['min_24h_volume']:
                        continue

                    # 2. Strategy-specific filter checks
                    rvol_ok = rvol >= profile['rvol_threshold']
                    rsi_1h_ok = rsi_1h <= profile['rsi_1h_max']
                    if strategy_name == "REVERSAL":
                        rsi_ok = rsi <= profile['rsi_max']
                        change_ok = recent_change <= profile['min_change']
                    else:
                        rsi_ok = profile['rsi_min'] <= rsi <= profile['rsi_max']
                        change_ok = recent_change >= profile['min_change']

                    if not (rvol_ok and rsi_ok and rsi_1h_ok and change_ok):
                        continue

                    # 3. Correlation guard — per (symbol, strategy) combination
                    allowed_corr, corr_reason = portfolio_lib.check_correlation_guard(
                        open_orders_for_corr, symbol
                    )
                    if not allowed_corr:
                        log.debug(f"Filter: {symbol}/{strategy_name} skipped — correlation guard: {corr_reason}")
                        continue

                    # 4. Pre-filter: skip full bearish EMA stack for momentum strategies.
                    if strategy_name != "REVERSAL" and ema_15m.get('alignment') == "BEARISH":
                        log.debug(f"Filter: {symbol} skipped — bearish 15m EMA stack in {strategy_name} mode")
                        continue

                    # 5. 4h EMA mandatory trend gate: reject entries against the dominant 4h trend.
                    # CONSERVATIVE: rejects pure BEARISH only — WEAKENING allowed (pullback in
                    #   broader uptrend, or relative-strength symbol in a weak market)
                    # AGGRESSIVE: rejects BEARISH only (unchanged)
                    # REVERSAL: exempt (counter-trend by design — expects bearish 4h structure)
                    if ema_4h_align:
                        if strategy_name == "CONSERVATIVE" and ema_4h_align == "BEARISH":
                            log.debug(f"Filter: {symbol} skipped — 4h EMA BEARISH in CONSERVATIVE mode")
                            continue
                        elif strategy_name == "AGGRESSIVE" and ema_4h_align == "BEARISH":
                            log.debug(f"Filter: {symbol} skipped — 4h EMA BEARISH in AGGRESSIVE mode")
                            continue

                    # Build candidate payload for this (symbol, strategy) pair
                    candidate = dict(data)
                    candidate['symbol'] = symbol
                    candidate['strategy_name'] = strategy_name
                    candidate['rvol'] = round(rvol, 2)
                    candidate['rsi'] = rsi
                    candidate['rsi_1h'] = rsi_1h
                    candidate['recent_change'] = recent_change
                    candidate.update(indicators)
                    candidate['filter_score'] = self._score_candidate(candidate, strategy_name)

                    log.info(
                        f"Filter Match: {symbol}/{strategy_name} | Score: {candidate['filter_score']} "
                        f"| RVOL: {rvol:.2f} | RSI(15m): {rsi} | RSI(1h): {rsi_1h} "
                        f"| 4h Chg: {recent_change:+.2f}% "
                        f"| EMA(15m): {ema_15m.get('alignment', 'N/A')} "
                        f"| EMA(1h): {(indicators.get('ema_stack_1h') or {}).get('alignment', 'N/A')} "
                        f"| EMA(4h): {ema_4h_align or 'N/A'}"
                    )
                    filtered_candidates.append(candidate)

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
