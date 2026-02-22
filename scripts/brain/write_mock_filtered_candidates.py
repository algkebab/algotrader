#!/usr/bin/env python3
"""
Write properly shaped mock filtered_candidates to Redis for testing Brain.

Brain expects each item: symbol, last_price, rsi, rvol, candles.
Candles: list of [timestamp, open, high, low, close, volume]; at least 5 used in AI prompt.
"""
import argparse
import json
import os
import sys
import time

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
from _redis import get_redis


def make_candles(base_price: float, count: int = 24):
    """Return candles in Brain format: [ts, open, high, low, close, volume]."""
    now = int(time.time())
    return [
        [now - (count - i) * 3600, base_price, base_price + 50, base_price - 30, base_price + 10, 1000.0]
        for i in range(count)
    ]


def main():
    parser = argparse.ArgumentParser(
        description="Write mock filtered_candidates for Brain (proper shape for get_ai_verdict)"
    )
    parser.add_argument(
        "--symbols",
        default="BTC/USDT,ETH/USDT",
        help="Comma-separated symbols (default: BTC/USDT,ETH/USDT)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Delete cache:brain_price:* so Brain will analyze (no skip)",
    )
    parser.add_argument(
        "--set-cache-skip",
        action="store_true",
        help="Set cache to same as mock price so Brain skips (test cache path)",
    )
    args = parser.parse_args()

    db = get_redis()
    db.ping()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    # Prices and RSI/RVOL per symbol (realistic shape)
    defaults = [
        ("BTC/USDT", 50000.0, 45, 2.5),
        ("ETH/USDT", 3000.0, 55, 2.0),
        ("SOL/USDT", 150.0, 38, 2.8),
    ]
    by_symbol = {s[0]: s[1:] for s in defaults}

    candidates = []
    for symbol in symbols:
        if symbol in by_symbol:
            base_price, rsi, rvol = by_symbol[symbol]
        else:
            base_price, rsi, rvol = 100.0, 50, 2.0
        candles = make_candles(base_price)
        item = {
            "symbol": symbol,
            "last_price": base_price,
            "change_24h": 2.0,
            "volume_24h": 20_000_000,
            "rsi": rsi,
            "rvol": rvol,
            "candles": candles,
        }
        candidates.append(item)

        if args.set_cache_skip:
            db.set(f"cache:brain_price:{symbol}", str(base_price), ex=1800)
        elif args.clear_cache:
            db.delete(f"cache:brain_price:{symbol}")

    db.set("filtered_candidates", json.dumps(candidates))
    print(f"Wrote {len(candidates)} mock filtered_candidates (Brain-ready shape).")
    if args.clear_cache:
        print("Cleared brain cache for these symbols → Brain will analyze.")
    if args.set_cache_skip:
        print("Set cache = mock price → Brain will skip (test cache path).")


if __name__ == "__main__":
    main()
