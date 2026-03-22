"""Scout service: data collection from exchanges."""
import asyncio
import json
import os
import sys

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
from shared import logger as shared_logger

load_dotenv()

log = shared_logger.get_logger("scout")


class Scout:
    def __init__(self):
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        self.db.ping()
        log.info(f"Scout: Connected to Redis at {redis_host}:6379")
        self.exchange = ccxt.binance({
            'enableRateLimit': True,
        })
        log.info("Scout: Binance client initialized (public API, rate limit enabled)")

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
            log.info(f"Scout: Refreshing TOP {max_symbols} assets by volume...")
            tickers = await self.exchange.fetch_tickers()

            log.info(f"Scout: Fetched {len(tickers)} tickers from exchange")
            # Filter only USDT pairs that are currently active
            usdt_pairs = [
                {'symbol': s, 'volume': t['quoteVolume']}
                for s, t in tickers.items()
                if s.endswith('/USDT') and t['quoteVolume'] is not None
            ]

            # Sort by volume descending and take the top ones
            sorted_pairs = sorted(usdt_pairs, key=lambda x: x['volume'], reverse=True)
            top = [p['symbol'] for p in sorted_pairs[:max_symbols]]
            log.info(f"Scout: Selected {len(top)} USDT pairs by volume (max_symbols={max_symbols})")
            top_tickers = {s: tickers[s] for s in top if s in tickers}
            return top, top_tickers

        except Exception as e:
            log.error(f"Scout: Error fetching symbols: {e}")
            log.warning("Scout: Using emergency fallback symbol list")
            fallback = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT']
            try:
                fallback_tickers = await self.exchange.fetch_tickers(fallback)
            except Exception as fe:
                log.error(f"Scout: Fallback ticker fetch also failed: {fe}")
                fallback_tickers = {}
            return fallback, fallback_tickers

    async def fetch_ohlcv_data(self, symbol, timeframe='15m', limit=50):
        """Fetches historical candlestick data (OHLCV) from the exchange.

        Returns list of [timestamp, open, high, low, close, volume] or [] on error.
        """
        try:
            ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            log.debug(f"Scout: OHLCV {symbol} {timeframe} -> {len(ohlcv)} candles")
            return ohlcv
        except Exception as e:
            log.error(f"Scout: Error fetching candles for {symbol} {timeframe}: {e}")
            return []

    async def run(self):
        log.info("Scout: Starting advanced market monitoring...")

        while True:
            symbols, tickers = await self.get_top_active_symbols()
            log.info(f"Scout: Scanning {len(symbols)} symbols for opportunities...")

            active_symbols = []

            for symbol in symbols:
                try:
                    ticker = tickers.get(symbol)
                    if not ticker:
                        log.warning(f"Scout: Missing ticker for {symbol}, skipping.")
                        continue

                    # 1. Use existing ticker data
                    last_price = ticker.get('last')
                    if last_price is None:
                        log.warning(f"Scout: {symbol} skipped — last price is None")
                        continue
                    change_24h = ticker.get('percentage')
                    volume_24h = ticker.get('quoteVolume')
                    high_24h = ticker.get('high')
                    low_24h = ticker.get('low')

                    # 2. Fetch candles for two timeframes:
                    #    - 15m (100 candles ≈ 25 h): entry signal computation
                    #      (RSI, MACD, Bollinger Bands, EMA9/21/50, VWAP, ATR)
                    #      100 candles ensures EMA50 has converged (needs ~2× period)
                    #    - 1h  (150 candles ≈ 6 days): higher-timeframe trend direction
                    #      150 candles ensures EMA50(1h) is meaningful
                    #    - 4h  (100 candles ≈ 17 days): mandatory trend gate
                    #      filters entries against the dominant 4h trend direction
                    candles_15m = await self.fetch_ohlcv_data(symbol, '15m', 100)
                    candles_1h = await self.fetch_ohlcv_data(symbol, '1h', 150)
                    candles_4h = await self.fetch_ohlcv_data(symbol, '4h', 100)

                    entry = {
                        'last_price': last_price,
                        'change_24h': change_24h,
                        'volume_24h': volume_24h,
                        'high_24h': high_24h,
                        'low_24h': low_24h,
                        'candles_15m': candles_15m,
                        'candles_1h': candles_1h,
                        'candles_4h': candles_4h,
                    }

                    # Save per-symbol market data; TTL = 3 Scout cycles so Filter
                    # naturally goes quiet if Scout crashes rather than scanning stale data
                    key = f"market_data:{symbol}"
                    self.db.set(key, json.dumps(entry), ex=360)
                    active_symbols.append(symbol)
                    log.info(f"Scout: Data updated & saved: {symbol}")

                    # Small delay to keep API weight usage low
                    log.debug(f"Scout: Rate limit pause 0.1s after {symbol}")
                    await asyncio.sleep(0.1)

                except Exception as e:
                    log.error(f"Scout: Error updating {symbol}: {e}")

            # Save active symbols list so other services know which keys to read
            try:
                self.db.set(shared_config.REDIS_KEY_ACTIVE_SYMBOLS, json.dumps(active_symbols), ex=360)
                log.info(f"Scout: Updated {shared_config.REDIS_KEY_ACTIVE_SYMBOLS} ({len(active_symbols)} symbols)")
            except Exception as e:
                log.error(f"Scout: Redis error saving active symbols: {e}")

            log.info(f"Scout: Scan complete. {len(active_symbols)} candidates. Sleeping 120s.")
            await asyncio.sleep(120)


if __name__ == "__main__":
    log.info("Scout: Service starting")
    scout = Scout()
    log.info("Scout: Entering main loop")
    asyncio.run(scout.run())
