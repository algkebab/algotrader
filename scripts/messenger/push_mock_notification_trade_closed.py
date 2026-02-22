#!/usr/bin/env python3
"""Push a mock trade_closed notification to Redis. Messenger (if running) will send it to Telegram."""
import argparse
import json
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
from _redis import get_redis


def main():
    parser = argparse.ArgumentParser(description="Push mock trade_closed notification for Messenger")
    parser.add_argument("--symbol", default="BTC/USDT", help="Symbol (default: BTC/USDT)")
    parser.add_argument("--pnl-usdt", type=float, default=12.5, help="PnL in USDT (default: 12.5)")
    parser.add_argument("--pnl-percent", type=float, default=2.5, help="PnL %% (default: 2.5)")
    parser.add_argument("--reason", default="TAKE-PROFIT 🟢", help="Close reason")
    args = parser.parse_args()

    db = get_redis()
    db.ping()

    notification = {
        "type": "trade_closed",
        "data": {
            "symbol": args.symbol,
            "pnl_usdt": args.pnl_usdt,
            "pnl_percent": args.pnl_percent,
            "reason": args.reason,
        },
    }
    db.rpush("notifications", json.dumps(notification))
    print(f"Pushed mock trade_closed: {args.symbol} ({args.pnl_usdt} USDT, {args.pnl_percent}%). Messenger will send to Telegram when running.")


if __name__ == "__main__":
    main()
