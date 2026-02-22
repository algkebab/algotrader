#!/usr/bin/env python3
"""Push multiple mock trade commands to Redis. Executor (if running) will process each one."""
import argparse
import json
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
from _redis import get_redis


def main():
    parser = argparse.ArgumentParser(description="Push multiple trade commands for Executor to process")
    parser.add_argument(
        "--symbols",
        default="BTC/USDT,ETH/USDT,SOL/USDT",
        help="Comma-separated symbols (default: BTC/USDT,ETH/USDT,SOL/USDT)",
    )
    parser.add_argument("--amount", type=float, default=10, help="Amount in USDT per symbol (default: 10)")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    db = get_redis()
    db.ping()

    for symbol in symbols:
        command = {"symbol": symbol, "amount": args.amount}
        db.rpush("trade_commands", json.dumps(command))
        print(f"Pushed trade_command: {command}")

    print(f"Total {len(symbols)} command(s). Executor will process in order when running.")


if __name__ == "__main__":
    main()
