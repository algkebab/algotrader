"""Shared Redis connection for scripts. Load .env from project root."""
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

from dotenv import load_dotenv

load_dotenv()

import redis


def get_redis():
    host = os.getenv("REDIS_HOST", "localhost")
    return redis.Redis(host=host, port=6379, decode_responses=True)
