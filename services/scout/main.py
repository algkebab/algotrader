"""Scout service: data collection from exchanges."""
import asyncio
import json
import os
from datetime import datetime

import ccxt.async_support as ccxt
import redis
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


def _ts():
    return datetime.utcnow().strftime("%H:%M:%S")


class Scout:
    def __init__(self):
        # Initialize asynchronous Binance exchange client
        # Use environment variable to switch between 'localhost' and 'redis'
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        self.db.ping()
        print(f"[{_ts()}] Scout: Connected to Redis at {redis_host}:6379")
        self.exchange = ccxt.binance({
            'apiKey': os.getenv('BINANCE_API_KEY'),
            'secret': os.getenv('BINANCE_SECRET'),
            'enableRateLimit': True,
        })
        print(f"[{_ts()}] Scout: Binance client initialized (rate limit enabled)")

    async def save_to_redis(self, data):
        """
        Saves normalized market data to Redis as a JSON string.
        """
        try:
            payload = json.dumps(data)
            self.db.set('market_data', payload)
            size_kb = len(payload) / 1024
            print(f"[{_ts()}] Scout: Saved {len(data)} USDT pairs to Redis (key: market_data, ~{size_kb:.1f} KB)")
        except Exception as e:
            print(f"[{_ts()}] Scout: Redis error: {e}")

    async def run(self):
        print(f"[{_ts()}] Scout: Starting continuous data collection (interval: 60s)...")
        try:
            loop_count = 0
            while True:
                loop_count += 1
                print(f"[{_ts()}] Scout: Fetching tickers from Binance (cycle #{loop_count})...")
                tickers = await self.exchange.fetch_tickers()
                usdt_pairs = {
                    symbol: {
                        'symbol': symbol,
                        'last_price': data['last'],
                        'change_24h': data['percentage'],
                        'volume_24h': data['quoteVolume'],
                    }
                    for symbol, data in tickers.items()
                    if symbol.endswith('/USDT')
                }
                print(f"[{_ts()}] Scout: Received {len(tickers)} tickers, {len(usdt_pairs)} USDT pairs")
                await self.save_to_redis(usdt_pairs)
                print(f"[{_ts()}] Scout: Sleeping 60s until next fetch...")
                await asyncio.sleep(60)
        except Exception as e:
            print(f"[{_ts()}] Scout: Loop error: {e}")
        finally:
            await self.exchange.close()
            print(f"[{_ts()}] Scout: Exchange client closed")

if __name__ == "__main__":
    scout = Scout()
    asyncio.run(scout.run())