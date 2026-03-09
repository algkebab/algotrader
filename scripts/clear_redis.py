#!/usr/bin/env python3
"""Clear Redis: by default flushes the entire DB. Use --algotrader-only to remove only app keys."""
import argparse
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
from _redis import get_redis

ALGOTRADER_KEYS = [
    "market_data",
    "filtered_candidates",
    "signals",
    "trade_commands",
    "active_trades",
    "notifications",
    "system:trading_paused",
    "system:suppress_wait_signals",
    "system:autopilot",
]
PATTERNS = ["analyzed:*", "last_vol:*", "cache:brain_price:*"]


def delete_pattern(db, pattern):
    count = 0
    for key in db.scan_iter(match=pattern):
        db.delete(key)
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description="Clear Redis (default: flush entire DB)")
    parser.add_argument(
        "--algotrader-only",
        action="store_true",
        help="Remove only algotrader keys; default is to flush entire DB",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Only print what would be deleted",
    )
    args = parser.parse_args()

    db = get_redis()
    db.ping()

    if not args.algotrader_only:
        if args.dry_run:
            key_count = db.dbsize()
            print(f"Would FLUSHDB (current DB has {key_count} keys)")
        else:
            db.flushdb()
            print("Redis flushed (all keys removed).")
        return

    deleted = 0
    if args.dry_run:
        for key in ALGOTRADER_KEYS:
            if db.exists(key):
                print(f"Would delete: {key}")
                deleted += 1
        for pattern in PATTERNS:
            for key in db.scan_iter(match=pattern):
                print(f"Would delete: {key}")
                deleted += 1
        print(f"Would delete {deleted} key(s).")
        return

    for key in ALGOTRADER_KEYS:
        if db.delete(key):
            deleted += 1
            print(f"Deleted: {key}")
    for pattern in PATTERNS:
        n = delete_pattern(db, pattern)
        deleted += n
        if n:
            print(f"Deleted {n} key(s) matching {pattern}")

    print(f"Done. Removed {deleted} key(s).")


if __name__ == "__main__":
    main()
