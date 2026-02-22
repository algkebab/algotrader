#!/usr/bin/env python3
"""Clear Brain price cache so next filtered_candidates trigger re-analysis (ignore 0.5% change threshold)."""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
from _redis import get_redis


def main():
    db = get_redis()
    db.ping()
    count = 0
    for key in db.scan_iter(match="cache:brain_price:*"):
        db.delete(key)
        count += 1
    print(f"Cleared {count} brain cache key(s). Brain will re-analyze on next filtered_candidates.")


if __name__ == "__main__":
    main()
