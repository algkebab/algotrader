"""Scout service: data collection from exchanges."""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import ccxt.async_support as ccxt
import redis
from dotenv import load_dotenv

# Allow importing shared (project root or /app in Docker)
_this_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.abspath(os.path.join(_this_dir, "..", "..")) if os.path.basename(_this_dir) == "scout" else _this_dir
if _root not in sys.path:
    sys.path.insert(0, _root)

from shared import config as shared_config
from shared import db as shared_db

load_dotenv()


def _ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


class Scout:
    def __init__(self):
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        self.db.ping()
        print(f"[{_ts()}] Scout: Connected to Redis at {redis_host}:6379")
        print(f"[{_ts()}] Scout: Redis ping OK")
        self.exchange = ccxt.binance({
            'enableRateLimit': True,
        })
        print(f"[{_ts()}] Scout: Binance client initialized (public API, rate limit enabled)")
    def _get_max_symbols(self):
        """Read max_symbols from DB (set by Messenger 'set symbols'); default from config, clamped."""
        val = shared_db.get_setting_value(shared_config.SYSTEM_KEY_MAX_SYMBOLS)
        if val is None:
            return shared_config.MAX_SYMBOLS_DEFAULT
        try:
            n = int(val)
            return max(shared_config.MAX_SYMBOLS_MIN, min(shared_config.MAX_SYMBOLS_MAX, n))
        except (ValueError, TypeError):
            return shared_config.MAX_SYMBOLS_DEFAULT

    async def get_top_active_symbols(self):
        """Fetches TOP coins by 24h volume to ensure we track volatile assets.

        Returns (symbols_list, tickers_dict_for_those_symbols).
        """
        max_symbols = self._get_max_symbols()
        try:
            print(f"[{_ts()}] 🔄 Scout: Refreshing TOP {max_symbols} assets by volume...")
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
            top = [p['symbol'] for p in sorted_pairs[:max_symbols]]
            print(f"[{_ts()}] Scout: Selected {len(top)} USDT pairs by volume (max_symbols={max_symbols})")
            top_tickers = {s: tickers[s] for s in top if s in tickers}
            return top, top_tickers
            
        except Exception as e:
            print(f"[{_ts()}] ❌ Error fetching symbols: {e}")
            print(f"[{_ts()}] Scout: Using emergency fallback symbol list")
            # Emergency fallback (no pre-fetched tickers)
            fallback = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT']
            return fallback, {}

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
            symbols, tickers = await self.get_top_active_symbols()
            print(f"[{_ts()}] 🛰️ Scanning {len(symbols)} symbols for opportunities...")

            active_symbols = []

            for symbol in symbols:
                try:
                    ticker = tickers.get(symbol)
                    if not ticker:
                        print(f"[{_ts()}] Scout: Missing ticker for {symbol}, skipping.")
                        continue

                    # 1. Use existing ticker data
                    last_price = ticker.get('last')
                    change_24h = ticker.get('percentage')
                    volume_24h = ticker.get('quoteVolume')
                    high_24h = ticker.get('high')
                    low_24h = ticker.get('low')

                    # 2. Get historical candles (last 24 hours)
                    candles = await self.fetch_ohlcv_data(symbol)

                    entry = {
                        'last_price': last_price,
                        'change_24h': change_24h,
                        'volume_24h': volume_24h,
                        'high_24h': high_24h,
                        'low_24h': low_24h,
                        'candles': candles,  # Nested candle data for AI/Filter analysis
                    }

                    # Save per-symbol market data
                    key = f"market_data:{symbol}"
                    self.db.set(key, json.dumps(entry))
                    active_symbols.append(symbol)
                    print(f"[{_ts()}] ✅ Data updated & saved: {symbol}")

                    # Small delay to keep API weight usage low
                    print(f"[{_ts()}] Scout: Rate limit pause 0.1s after {symbol}")
                    await asyncio.sleep(0.1)

                except Exception as e:
                    print(f"[{_ts()}] ❌ Error updating {symbol}: {e}")

            # Save active symbols list so other services know which keys to read
            try:
                self.db.set(shared_config.REDIS_KEY_ACTIVE_SYMBOLS, json.dumps(active_symbols))
                print(f"[{_ts()}] Scout: Updated {shared_config.REDIS_KEY_ACTIVE_SYMBOLS} ({len(active_symbols)} symbols)")
            except Exception as e:
                print(f"[{_ts()}] Scout: Redis error saving active symbols: {e}")

            print(f"[{_ts()}] ✅ Scan complete. Analyzing {len(active_symbols)} candidates.")
            print(f"[{_ts()}] Scout: Sleeping 120s until next cycle")
            await asyncio.sleep(120)
            
if __name__ == "__main__":
    print(f"[{_ts()}] Scout: Service starting")
    scout = Scout()
    print(f"[{_ts()}] Scout: Entering main loop")
    asyncio.run(scout.run())