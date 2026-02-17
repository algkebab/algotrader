"""Filter service: liquidity and trash filtering logic."""

import json
import os
import time

import redis


class Filter:
    def __init__(self):
        redis_host = os.getenv("REDIS_HOST", "localhost")
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        print(f"🛡️ Filter: Connected to Redis at {redis_host}:6379")
        # Criteria: Volume > $10M and Move > 3%
        self.min_volume = 10_000_000
        self.min_change = 3.0

    def run(self):
        print("🛡️ Filter: Monitoring Redis for candidates...")
        while True:
            raw_data = self.db.get('market_data')
            if raw_data:
                market_data = json.loads(raw_data)
                candidates = [
                    d
                    for d in market_data.values()
                    if (d.get("volume_24h") or 0) >= self.min_volume
                    and abs(d.get("change_24h") or 0) >= self.min_change
                ]
                
                if candidates:
                    print(f"\n🔥 {time.strftime('%H:%M:%S')} | Found {len(candidates)} hot assets:")
                    for c in sorted(candidates, key=lambda x: x["change_24h"] or 0, reverse=True):
                        print(f"   - {c['symbol']}: {c['change_24h']}% (Vol: ${c['volume_24h']:,.0f})")

                # Store filtered candidates for the next service (Brain/Messenger)
                self.db.set("filtered_candidates", json.dumps(candidates))
                print(f"💾 Filter: Wrote {len(candidates)} candidates to Redis (key: filtered_candidates)")
            
            time.sleep(15)

if __name__ == "__main__":
    f = Filter()
    f.run()