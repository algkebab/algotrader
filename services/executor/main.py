import redis
import json
import os
import ccxt
import time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

def _ts():
    """Returns current timestamp for logging."""
    return datetime.utcnow().strftime("%H:%M:%S")

class Executor:
    def __init__(self):
        # Redis setup
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        
        # Binance API setup
        self.exchange = ccxt.binance({
            'apiKey': os.getenv('BINANCE_API_KEY'),
            'secret': os.getenv('BINANCE_SECRET'),
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })
        
        # Default strategy settings
        self.tp_percent = 1.05  # +5%
        self.sl_percent = 0.98  # -2%

    def get_precision_amount(self, symbol, amount):
        """Adjusts the coin amount to the exchange's required precision."""
        market = self.exchange.market(symbol)
        return self.exchange.amount_to_precision(symbol, amount)

    def get_precision_price(self, symbol, price):
        """Adjusts the price to the exchange's required precision."""
        return self.exchange.price_to_precision(symbol, price)

    def place_smart_order(self, symbol, amount_usdt=10):
        """
        Executes a market buy and prepares SL/TP data.
        Note: Real orders are commented out for safety during testing.
        """
        try:
            print(f"[{_ts()}] 🛒 Executor: Processing SMART order for {symbol}")
            
            # 1. Load markets to get precision rules
            self.exchange.load_markets()
            
            # 2. Get current market price
            ticker = self.exchange.fetch_ticker(symbol)
            entry_price = ticker['last']
            
            # 3. Calculate target prices
            tp_price = self.get_precision_price(symbol, entry_price * self.tp_percent)
            sl_price = self.get_precision_price(symbol, entry_price * self.sl_percent)
            
            # 4. Calculate quantity of coins to buy
            raw_qty = amount_usdt / entry_price
            qty = self.get_precision_amount(symbol, raw_qty)

            print(f"[{_ts()}] 📊 Planned: Buy {qty} {symbol} @ {entry_price}")
            print(f"[{_ts()}] 🎯 Targets: TP: {tp_price} | SL: {sl_price}")

            # --- SIMULATION MODE ---
            # In a real scenario, you would uncomment these lines:
            # buy_order = self.exchange.create_market_buy_order(symbol, qty)
            # print(f"[{_ts()}] ✅ Real Market Buy executed")
            
            # 5. Prepare result notification
            result = {
                "status": "success",
                "symbol": symbol,
                "entry": entry_price,
                "qty": qty,
                "tp": tp_price,
                "sl": sl_price
            }
            
            # Notify Messenger through Redis queue
            self.db.rpush('notifications', json.dumps({
                "type": "trade_confirmed",
                "data": result
            }))
            
            return result

        except Exception as e:
            print(f"[{_ts()}] ❌ Executor Error: {e}")
            return {"status": "error", "message": str(e)}

    def run(self):
        print(f"[{_ts()}] ⚡ Executor: Waiting for trade commands from Redis...")
        
        while True:
            # Listen for 'trade_commands' queue
            command_data = self.db.blpop('trade_commands', timeout=10)
            
            if command_data:
                _, payload = command_data
                try:
                    data = json.loads(payload)
                    symbol = data.get('symbol')
                    amount = data.get('amount', 10)
                    
                    self.place_smart_order(symbol, amount)
                    
                except Exception as e:
                    print(f"[{_ts()}] ❌ Error parsing command: {e}")

if __name__ == "__main__":
    executor = Executor()
    executor.run()