"""Executor service: trade execution on exchange."""
import json
import os
from datetime import datetime

import ccxt
import redis
from dotenv import load_dotenv

load_dotenv()


def _ts():
    return datetime.utcnow().strftime("%H:%M:%S")


class Executor:
    def __init__(self):
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        self.db.ping()
        print(f"[{_ts()}] Executor: Connected to Redis at {redis_host}:6379")
        self.exchange = ccxt.binance({
            'apiKey': os.getenv('BINANCE_API_KEY'),
            'secret': os.getenv('BINANCE_SECRET'),
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'},
        })
        print(f"[{_ts()}] Executor: Binance client initialized (spot, rate limit enabled)")

    def place_market_buy(self, symbol, amount_usdt=10):
        """Places a market buy order (simulated by default)."""
        try:
            print(f"[{_ts()}] Executor: Placing market buy: {amount_usdt} USDT of {symbol}...")
            # For real trading, use:
            # order = self.exchange.create_market_buy_order(symbol, amount_usdt)
            # return order
            result = {"status": "success", "msg": f"Simulated buy of {symbol}"}
            print(f"[{_ts()}] Executor: Order result: {result['status']} — {result['msg']}")
            return result
        except Exception as e:
            print(f"[{_ts()}] Executor: Trade error: {e}")
            return {"status": "error", "msg": str(e)}

    def run(self):
        print(f"[{_ts()}] Executor: Listening for trade_commands (timeout 10s)...")
        idle_cycles = 0
        while True:
            command = self.db.blpop('trade_commands', timeout=10)
            if not command:
                idle_cycles += 1
                if idle_cycles == 1 or idle_cycles % 30 == 0:
                    print(f"[{_ts()}] Executor: No command (idle cycle #{idle_cycles})")
                continue

            idle_cycles = 0
            _, payload = command
            data = json.loads(payload)
            symbol = data.get('symbol', '?')
            amount = data.get('amount', 10)
            print(f"[{_ts()}] Executor: Command received: symbol={symbol}, amount={amount} USDT")
            result = self.place_market_buy(symbol, amount)
            result_key = f"trade_result:{symbol}"
            self.db.set(result_key, json.dumps(result), ex=60)
            print(f"[{_ts()}] Executor: Wrote {result_key} to Redis (ttl 60s)")


if __name__ == "__main__":
    executor = Executor()
    executor.run()
