#!/usr/bin/env python3
"""Write mock filtered_candidates to Redis so Brain can run without Filter."""
import json
import os
import sys
import time

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
from _redis import get_redis


def main():
    db = get_redis()
    db.ping()

    mock = [
        {
            "symbol": "BTC/USDT",
            "last_price": 50000,
            "change_24h": 2.5,
            "volume_24h": 50_000_000,
            "rsi": 45,
            "rvol": 2.5,
            "candles": [[int(time.time()) - 3600 * i, 50000, 50100, 49900, 50050, 1000] for i in range(20)],
        },
        {
            "symbol": "ETH/USDT",
            "last_price": 3000,
            "change_24h": 1.2,
            "volume_24h": 20_000_000,
            "rsi": 55,
            "rvol": 2.0,
            "candles": [[int(time.time()) - 3600 * i, 3000, 3020, 2980, 3010, 500] for i in range(20)],
        },
    ]
    db.set("filtered_candidates", json.dumps(mock))
    print(f"Wrote mock filtered_candidates ({len(mock)} items) for Brain to consume.")


if __name__ == "__main__":
    main()
