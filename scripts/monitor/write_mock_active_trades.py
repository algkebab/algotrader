#!/usr/bin/env python3
"""Write mock active_trades to Redis so Monitor has positions to track."""
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

    # Same shape Executor writes: status, symbol, entry, qty, tp, sl, timestamp
    trades = {
        "BTC/USDT": {
            "status": "success",
            "symbol": "BTC/USDT",
            "entry": 50000.0,
            "qty": 0.0002,
            "tp": 52500.0,
            "sl": 49000.0,
            "timestamp": time.time(),
        },
        "ETH/USDT": {
            "status": "success",
            "symbol": "ETH/USDT",
            "entry": 3000.0,
            "qty": 0.003,
            "tp": 3150.0,
            "sl": 2940.0,
            "timestamp": time.time(),
        },
    }
    for symbol, data in trades.items():
        db.hset("active_trades", symbol, json.dumps(data))
    print(f"Wrote mock active_trades ({len(trades)} symbols) for Monitor to track.")


if __name__ == "__main__":
    main()
