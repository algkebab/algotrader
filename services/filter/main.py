import json
import os
import sys
import time

import redis

# Allow importing shared.db (project root or /app in Docker)
_this_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.abspath(os.path.join(_this_dir, "..", "..")) if os.path.basename(_this_dir) == "filter" else _this_dir
if _root not in sys.path:
    sys.path.insert(0, _root)

from shared import db as shared_db
from shared import config as shared_config


# Hardcoded strategy profiles: min_24h_volume, rvol_threshold, rsi_max, min_change (%)
# REVERSAL: rsi_max used as oversold threshold (RSI < 30), min_change negative (price drop)
STRATEGY_PROFILES = {
    "CONSERVATIVE": {
        "min_24h_volume": 10_000_000,
        "rvol_threshold": 2.0,
        "rsi_max": 70,
        "min_change": 1.5,
    },
    "AGGRESSIVE": {
        "min_24h_volume": 5_000_000,
        "rvol_threshold": 1.5,
        "rsi_max": 85,
        "min_change": 3.0,
    },
    "REVERSAL": {
        "min_24h_volume": 20_000_000,
        "rvol_threshold": 4.0,
        "rsi_max": 30,   # oversold: look for RSI < 30
        "min_change": -5.0,  # negative: look for price drop >= 5%
    },
}
STRATEGY_DEFAULT = "CONSERVATIVE"


class Filter:
    def __init__(self):
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        self.rsi_period = 14

    def calculate_rsi(self, candles):
        """
        Calculates the Relative Strength Index (RSI)
        Input: list of candles [timestamp, open, high, low, close, volume]
        """
        if len(candles) < self.rsi_period + 1:
            return 50  # Neutral if not enough data
        
        closes = [c[4] for c in candles] # Extract close prices
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]
        
        avg_gain = sum(gains[-self.rsi_period:]) / self.rsi_period
        avg_loss = sum(losses[-self.rsi_period:]) / self.rsi_period
        
        if avg_loss == 0:
            return 100
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return round(rsi, 2)

    def _get_max_open_orders(self):
        """Return max simultaneous open orders from DB (default 10)."""
        val = shared_db.get_setting_value(shared_config.SYSTEM_KEY_MAX_OPEN_ORDERS)
        if val is None or not str(val).isdigit():
            return shared_config.MAX_OPEN_ORDERS_DEFAULT
        return max(shared_config.MAX_OPEN_ORDERS_MIN, min(shared_config.MAX_OPEN_ORDERS_MAX, int(val)))

    def _get_open_order_count(self):
        """Return number of open orders in DB."""
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                return len(shared_db.get_open_orders(conn))
        except Exception:
            return 0

    def _get_strategy(self):
        """Return current strategy name and profile. Default CONSERVATIVE."""
        val = shared_db.get_setting_value(shared_config.SYSTEM_KEY_STRATEGY)
        name = (val or STRATEGY_DEFAULT).strip().upper()
        if name not in STRATEGY_PROFILES:
            name = STRATEGY_DEFAULT
        return name, STRATEGY_PROFILES[name]

    def calculate_rvol(self, current_vol_24h):
        """
        Simplified Relative Volume calculation.
        In a real app, we would compare current 1m vol vs average 1m vol.
        Here, we track changes in the 24h volume key over time.
        """
        # We'll use Redis to store the 'previous' volume to see the delta
        return 1.0 # Placeholder for the logic below

    def run(self):
        print("🛡️ Filter: Analyzing Volume & RSI indicators...")
        PAUSED_KEY = shared_config.SYSTEM_KEY_TRADING_PAUSED

        while True:
            if shared_db.get_setting_value(PAUSED_KEY) == "1":
                time.sleep(5)
                continue

            # Stop filtering when at max open orders (no new orders would be placed)
            open_count = self._get_open_order_count()
            max_open = self._get_max_open_orders()
            if open_count >= max_open:
                print(f"🛡️ Filter: Idle (max open orders reached: {open_count}/{max_open})")
                time.sleep(10)
                continue

            strategy_name, profile = self._get_strategy()
            print(f"🛡️ Filter: Scan cycle — strategy: {strategy_name}")

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
            pipe = self.db.pipeline()
            keys = []
            for symbol in symbols:
                keys.append(symbol)
                pipe.get(f"market_data:{symbol}")
            results = pipe.execute()

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

                # 2. RVOL Calculation (Relative Volume)
                last_vol_key = f"last_vol:{symbol}"
                prev_vol = self.db.get(last_vol_key)
                rvol = 0
                
                if prev_vol:
                    added_vol = data['volume_24h'] - float(prev_vol)
                    avg_vol_per_min = data['volume_24h'] / 1440
                    rvol = added_vol / avg_vol_per_min if avg_vol_per_min > 0 else 0
                
                self.db.set(last_vol_key, data['volume_24h'], ex=300)

                # 3. RSI Indicator
                rsi = self.calculate_rsi(data.get('candles', []))
                change_24h = data.get('change_24h')
                if change_24h is None:
                    change_24h = 0.0
                try:
                    change_24h = float(change_24h)
                except (TypeError, ValueError):
                    change_24h = 0.0

                # 4. Strategy-specific logic
                rvol_ok = rvol >= profile['rvol_threshold']
                if strategy_name == "REVERSAL":
                    # Oversold (RSI < 30) and significant price drop (change <= -5%)
                    rsi_ok = rsi <= profile['rsi_max']
                    change_ok = change_24h <= profile['min_change']
                else:
                    # Not overbought (RSI <= rsi_max) and min positive move
                    rsi_ok = rsi <= profile['rsi_max']
                    change_ok = change_24h >= profile['min_change']

                if rvol_ok and rsi_ok and change_ok:
                    # Skip if we already have an open order for this symbol
                    try:
                        with shared_db.get_connection() as conn:
                            shared_db.init_schema(conn)
                            if shared_db.get_open_order_id_for_symbol(conn, symbol) is not None:
                                continue
                    except Exception:
                        pass
                    print(f"✅ Filter Match: {symbol} | RVOL: {rvol:.2f} | RSI: {rsi}")
                    data['symbol'] = symbol
                    data['rvol'] = round(rvol, 2)
                    data['rsi'] = rsi
                    filtered_candidates.append(data)

            if filtered_candidates:
                # Re-check before writing (avoid race: 10th order opened during this loop)
                if self._get_open_order_count() >= self._get_max_open_orders():
                    pass  # Don't write; next cycle Filter will stay idle
                else:
                    self.db.set('filtered_candidates', json.dumps(filtered_candidates))
            
            time.sleep(10)

if __name__ == "__main__":
    f = Filter()
    f.run()