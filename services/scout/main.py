"""Scout service: data collection from exchanges."""
import ccxt.async_support as ccxt
import asyncio
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class Scout:
    def __init__(self):
        # Initialize asynchronous Binance exchange client
        self.exchange = ccxt.binance({
            'apiKey': os.getenv('BINANCE_API_KEY'),
            'secret': os.getenv('BINANCE_SECRET'),
            'enableRateLimit': True,
        })

    async def fetch_market_data(self):
        """
        Connects to Binance and retrieves 24h ticker data for all pairs.
        """
        try:
            print("🔍 Scout: Fetching market data from Binance...")
            # Fetch all tickers (24h statistics)
            tickers = await self.exchange.fetch_tickers()
            
            # Filter and normalize: focusing only on USDT pairs
            usdt_pairs = {
                symbol: {
                    'symbol': symbol,
                    'last_price': data['last'],
                    'change_24h': data['percentage'],
                    'volume_24h': data['quoteVolume']  # Trading volume in USDT
                }
                for symbol, data in tickers.items() if symbol.endswith('/USDT')
            }
            
            print(f"✅ Scout: Found {len(usdt_pairs)} USDT pairs.")
            return usdt_pairs

        except Exception as e:
            print(f"❌ Scout Error: {e}")
            return {}
        finally:
            # Always close the exchange connection to prevent leaks
            await self.exchange.close()

async def main():
    scout = Scout()
    data = await scout.fetch_market_data()
    
    # Test output: sort by volume and show top 3
    if data:
        top_volume = sorted(data.values(), key=lambda x: x['volume_24h'], reverse=True)[:3]
        print(f"📊 Top 3 by Volume: {top_volume}")
    else:
        print("⚠️ No data received.")

if __name__ == "__main__":
    asyncio.run(main())