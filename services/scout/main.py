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

        # Symbols to monitor (can be expanded)
        self.symbols = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'AVAX/USDT']

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

    async def fetch_ohlcv_data(self, symbol, timeframe='1h', limit=24):
        """Fetches historical candlestick data from the exchange"""
        try:
            # Fetch OHLCV: timestamp, open, high, low, close, volume
            ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            return ohlcv
        except Exception as e:
            print(f"Error fetching candles for {symbol}: {e}")
            return []

    async def run(self):
        print("🔭 Scout: Starting advanced market monitoring...")
        
        while True:
            market_summary = {}
            
            for symbol in self.symbols:
                try:
                    # 1. Get current ticker
                    ticker = await self.exchange.fetch_ticker(symbol)
                    
                    # 2. Get historical candles (last 24 hours)
                    candles = await self.fetch_ohlcv_data(symbol)
                    
                    market_summary[symbol] = {
                        'last_price': ticker['last'],
                        'change_24h': ticker['percentage'],
                        'volume_24h': ticker['quoteVolume'],
                        'candles': candles  # Nested candle data for AI/Filter analysis
                    }
                    print(f"✅ Data updated: {symbol}")
                    
                except Exception as e:
                    print(f"❌ Error updating {symbol}: {e}")
            
            # Save the enriched data to Redis
            self.db.set('market_data', json.dumps(market_summary))
            await asyncio.sleep(60)
            
if __name__ == "__main__":
    scout = Scout()
    asyncio.run(scout.run())