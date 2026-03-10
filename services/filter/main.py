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

# Redis key for max open orders (set by Messenger "orders set max"); default 10
REDIS_KEY_MAX_OPEN_ORDERS = "system:max_open_orders"
MAX_OPEN_ORDERS_DEFAULT = 10


class Filter:
    def __init__(self):
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)        

        # Configuration thresholds
        self.min_24h_volume = 10_000_000
        self.rvol_threshold = 2.0
        self.min_change = 1.5             # 1.5% min price move
        self.rsi_max = 70  # Do not enter if RSI is above 70 (overbought)
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
        """Return max simultaneous open orders from Redis (default 10)."""
        val = self.db.get(REDIS_KEY_MAX_OPEN_ORDERS)
        if val is None or not str(val).isdigit():
            return MAX_OPEN_ORDERS_DEFAULT
        return max(1, min(50, int(val)))

    def _get_open_order_count(self):
        """Return number of open orders in DB."""
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                return len(shared_db.get_open_orders(conn))
        except Exception:
            return 0

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
        PAUSED_KEY = "system:trading_paused"

        while True:
            if self.db.get(PAUSED_KEY):
                time.sleep(5)
                continue

            # Stop filtering when at max open orders (no new orders would be placed)
            open_count = self._get_open_order_count()
            max_open = self._get_max_open_orders()
            if open_count >= max_open:
                print(f"🛡️ Filter: Idle (max open orders reached: {open_count}/{max_open})")
                time.sleep(10)
                continue

            raw_data = self.db.get('market_data')
            if not raw_data:
                time.sleep(5)
                continue

            market_data = json.loads(raw_data)
            filtered_candidates = []

            for symbol, data in market_data.items():
                # 1. Volume Check
                if data['volume_24h'] < self.min_24h_volume:
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
                
                # FINAL LOGIC: Strong volume spike AND not overbought
                if rvol >= self.rvol_threshold and rsi <= self.rsi_max:
                    print(f"✅ Filter Match: {symbol} | RVOL: {rvol:.2f} | RSI: {rsi}")
                    data['symbol'] = symbol
                    data['rvol'] = round(rvol, 2)
                    data['rsi'] = rsi
                    filtered_candidates.append(data)

            if filtered_candidates:
                # Save to a new key for Brain to analyze
                self.db.set('filtered_candidates', json.dumps(filtered_candidates))
            
            time.sleep(10)

if __name__ == "__main__":
    f = Filter()
    f.run()