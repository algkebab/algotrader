"""Scout service: data collection from exchanges."""
import asyncio
import json
import os

import ccxt.async_support as ccxt
import redis
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class Scout:
    def __init__(self):
        # Initialize asynchronous Binance exchange client
        # Use environment variable to switch between 'localhost' and 'redis'
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        self.exchange = ccxt.binance({
            'apiKey': os.getenv('BINANCE_API_KEY'),
            'secret': os.getenv('BINANCE_SECRET'),
            'enableRateLimit': True,
        })

    async def save_to_redis(self, data):
        """
        Saves normalized market data to Redis as a JSON string.
        """
        try:
            self.db.set('market_data', json.dumps(data))
            print("💾 Scout: Data saved to Redis.")
        except Exception as e:
            print(f"❌ Redis Error: {e}")

    async def run(self):
            """Main loop for data collection"""
            print("🚀 Scout: Starting continuous data collection...")
            try:
                while True:
                    # Fetching data
                    tickers = await self.exchange.fetch_tickers()
                    usdt_pairs = {
                        symbol: {
                            'symbol': symbol,
                            'last_price': data['last'],
                            'change_24h': data['percentage'],
                            'volume_24h': data['quoteVolume']
                        }
                        for symbol, data in tickers.items() if symbol.endswith('/USDT')
                    }
                    
                    # Saving data
                    await self.save_to_redis(usdt_pairs)
                    
                    # Wait for 60 seconds (Binance rate limits safety)
                    await asyncio.sleep(60)
            except Exception as e:
                print(f"❌ Scout Loop Error: {e}")
            finally:
                await self.exchange.close()

if __name__ == "__main__":
    scout = Scout()
    asyncio.run(scout.run())