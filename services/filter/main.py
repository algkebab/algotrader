import redis
import json
import os
import time

class Filter:
    def __init__(self):
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        
        # Thresholds
        self.min_24h_volume = 10_000_000  # $10M min daily volume
        self.min_change = 1.5             # 1.5% min price move
        self.rvol_threshold = 2.5         # Volume spike must be 2.5x above average

    def calculate_rvol(self, current_vol_24h):
        """
        Simplified Relative Volume calculation.
        In a real app, we would compare current 1m vol vs average 1m vol.
        Here, we track changes in the 24h volume key over time.
        """
        # We'll use Redis to store the 'previous' volume to see the delta
        return 1.0 # Placeholder for the logic below

    def run(self):
        print("🛡️ Filter: Volume Spike Detection active...")
        
        while True:
            raw_data = self.db.get('market_data')
            if not raw_data:
                time.sleep(5)
                continue

            market_data = json.loads(raw_data)
            hot_candidates = []

            for symbol, data in market_data.items():
                # 1. Basic Volume Filter
                if data['volume_24h'] < self.min_24h_volume:
                    continue

                # 2. Price Action Filter
                if abs(data['change_24h']) < self.min_change:
                    continue

                # 3. Volume Spike Logic (RVOL)
                # We store the 'last seen volume' in Redis for each symbol
                last_vol_key = f"last_vol:{symbol}"
                prev_vol = self.db.get(last_vol_key)
                
                if prev_vol:
                    prev_vol = float(prev_vol)
                    # How much volume was added since the last Scout update (typically 1 min)
                    added_vol = data['volume_24h'] - prev_vol
                    
                    # Average volume per minute over 24h
                    avg_vol_per_min = data['volume_24h'] / 1440
                    
                    if avg_vol_per_min > 0:
                        rvol = added_vol / avg_vol_per_min
                        data['rvol'] = round(rvol, 2)
                        
                        # If volume in the last minute is > 2.5x the average
                        if rvol >= self.rvol_threshold:
                            print(f"🔥 SPIKE: {symbol} RVOL: {rvol}")
                            hot_candidates.append(data)

                # Update the 'last seen volume' for the next cycle
                self.db.set(last_vol_key, data['volume_24h'], ex=300)

            if hot_candidates:
                self.db.set('filtered_candidates', json.dumps(hot_candidates))
            
            time.sleep(10)

if __name__ == "__main__":
    f = Filter()
    f.run()