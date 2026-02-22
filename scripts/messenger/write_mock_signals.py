#!/usr/bin/env python3
"""Write mock signals to Redis so Messenger will send Telegram alerts (if running)."""
import json
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
from _redis import get_redis


def main():
    db = get_redis()
    db.ping()

    signal = {
        "symbol": "BTC/USDT",
        "last_price": 50000,
        "verdict": "BUY",
        "reason": "Mock signal for testing",
        "confidence": "75%",
    }
    db.rpush("signals", json.dumps(signal))
    print("Pushed mock signal to Redis (Messenger will send to Telegram when running).")


if __name__ == "__main__":
    main()
