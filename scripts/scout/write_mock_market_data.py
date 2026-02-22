#!/usr/bin/env python3
"""Write mock market_data to Redis so Filter can run without Scout."""
import json
import os
import sys
import time

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
from _redis import get_redis

# Minimal candle: [timestamp, open, high, low, close, volume]
def make_candles(base_price=50000, count=25):
    return [
        [int(time.time() - (count - i) * 3600), base_price, base_price + 100, base_price - 50, base_price + 20, 1000]
        for i in range(count)
    ]


def main():
    db = get_redis()
    db.ping()

    mock = {
        "BTC/USDT": {
            "last_price": 50000,
            "change_24h": 2.5,
            "volume_24h": 50_000_000,
            "candles": make_candles(50000),
        },
        "ETH/USDT": {
            "last_price": 3000,
            "change_24h": 1.2,
            "volume_24h": 20_000_000,
            "candles": make_candles(3000, 25),
        },
    }
    db.set("market_data", json.dumps(mock))
    print(f"Wrote mock market_data ({len(mock)} symbols) for Filter to consume.")


if __name__ == "__main__":
    main()
