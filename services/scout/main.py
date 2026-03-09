"""Scout service: data collection from exchanges."""
import asyncio
import json
import os
import time
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
        print(f"[{_ts()}] Scout: Redis ping OK")
        # Public API only (no keys). Tickers/OHLCV are public; keys trigger signed requests
        # and -1021 "Timestamp outside recvWindow" when container clock drifts.
        self.exchange = ccxt.binance({
            'enableRateLimit': True,
        })
        print(f"[{_ts()}] Scout: Binance client initialized (public API, rate limit enabled)")
        self.max_symbols = 30
        print(f"[{_ts()}] Scout: max_symbols={self.max_symbols}")

    async def get_top_active_symbols(self):
        """Fetches TOP coins by 24h volume to ensure we track volatile assets."""
        try:
            print(f"[{_ts()}] 🔄 Scout: Refreshing TOP {self.max_symbols} assets by volume...")
            tickers = await self.exchange.fetch_tickers()
            
            print(f"[{_ts()}] Scout: Fetched {len(tickers)} tickers from exchange")
            # Filter only USDT pairs that are currently active
            usdt_pairs = [
                {'symbol': s, 'volume': t['quoteVolume']} 
                for s, t in tickers.items() 
                if s.endswith('/USDT') and t['quoteVolume'] is not None
            ]
            
            # Sort by volume descending and take the top ones
            sorted_pairs = sorted(usdt_pairs, key=lambda x: x['volume'], reverse=True)
            top = [p['symbol'] for p in sorted_pairs[:self.max_symbols]]
            print(f"[{_ts()}] Scout: Selected {len(top)} USDT pairs by volume")
            return top
            
        except Exception as e:
            print(f"[{_ts()}] ❌ Error fetching symbols: {e}")
            print(f"[{_ts()}] Scout: Using emergency fallback symbol list")
            return ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT'] # Emergency fallback

    async def save_to_redis(self, data):
        """
        Saves normalized market data to Redis as a JSON string.
        """
        try:
            print(f"[{_ts()}] Scout: Building payload for {len(data)} symbols")
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
            print(f"[{_ts()}] Scout: OHLCV {symbol} {timeframe} -> {len(ohlcv)} candles")
            return ohlcv
        except Exception as e:
            print(f"Error fetching candles for {symbol}: {e}")
            return []

    async def run(self):
        print("🔭 Scout: Starting advanced market monitoring...")
        
        while True:
            market_summary = {}
            active_symbols = await self.get_top_active_symbols()
            
            print(f"[{_ts()}] 🛰️ Scanning {len(active_symbols)} symbols for opportunities...")

            for symbol in active_symbols:
                try:
                    # 1. Get current ticker
                    print(f"[{_ts()}] Scout: Fetching ticker {symbol}")
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

                    # Small delay to keep API weight usage low
                    print(f"[{_ts()}] Scout: Rate limit pause 0.1s after {symbol}")
                    time.sleep(0.1)
                    
                except Exception as e:
                    print(f"❌ Error updating {symbol}: {e}")
            
            # Save the enriched data to Redis
            print(f"[{_ts()}] Scout: Writing market_data to Redis ({len(market_summary)} symbols)")
            self.db.set('market_data', json.dumps(market_summary))
            print(f"[{_ts()}] ✅ Scan complete. Analyzing {len(market_summary)} candidates.")
            print(f"[{_ts()}] Scout: Sleeping 120s until next cycle")
            await asyncio.sleep(120)
            
if __name__ == "__main__":
    print(f"[{_ts()}] Scout: Service starting")
    scout = Scout()
    print(f"[{_ts()}] Scout: Entering main loop")
    asyncio.run(scout.run())