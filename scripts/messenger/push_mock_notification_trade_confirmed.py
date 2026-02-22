#!/usr/bin/env python3
"""Push a mock trade_confirmed notification to Redis. Messenger (if running) will send it to Telegram."""
import argparse
import json
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
from _redis import get_redis


def main():
    parser = argparse.ArgumentParser(description="Push mock trade_confirmed notification for Messenger")
    parser.add_argument("--symbol", default="BTC/USDT", help="Symbol (default: BTC/USDT)")
    parser.add_argument("--entry", type=float, default=50000.0, help="Entry price")
    parser.add_argument("--tp", type=float, default=52500.0, help="Take profit price")
    parser.add_argument("--sl", type=float, default=49000.0, help="Stop loss price")
    args = parser.parse_args()

    db = get_redis()
    db.ping()

    notification = {
        "type": "trade_confirmed",
        "data": {
            "symbol": args.symbol,
            "entry": args.entry,
            "tp": args.tp,
            "sl": args.sl,
        },
    }
    db.rpush("notifications", json.dumps(notification))
    print(f"Pushed mock trade_confirmed: {args.symbol} entry={args.entry} TP={args.tp} SL={args.sl}. Messenger will send to Telegram when running.")


if __name__ == "__main__":
    main()
