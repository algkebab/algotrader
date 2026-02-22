#!/usr/bin/env python3
"""Push one mock trade command to Redis. Executor (if running) will process it."""
import argparse
import json
import os
import sys

# Run from project root: python scripts/executor/push_mock_trade_command.py
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
from _redis import get_redis


def main():
    parser = argparse.ArgumentParser(description="Push one trade command for Executor to process")
    parser.add_argument("--symbol", default="BTC/USDT", help="Symbol (default: BTC/USDT)")
    parser.add_argument("--amount", type=float, default=10, help="Amount in USDT (default: 10)")
    args = parser.parse_args()

    db = get_redis()
    db.ping()

    command = {"symbol": args.symbol, "amount": args.amount}
    db.rpush("trade_commands", json.dumps(command))
    print(f"Pushed trade_command: {command} (Executor will process when running)")


if __name__ == "__main__":
    main()
