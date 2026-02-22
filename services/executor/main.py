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
        return float(self.exchange.amount_to_precision(symbol, amount))

    def get_precision_price(self, symbol, price):
        """Adjusts the price to the exchange's required precision."""
        return float(self.exchange.price_to_precision(symbol, price))

    def get_free_usdt_balance(self):
        """Checks the available USDT balance on the Binance Spot account."""
        try:
            # For Paper Trading, we might want to return a virtual balance if real is 0
            balance = self.exchange.fetch_balance()
            free_balance = float(balance['total'].get('USDT', 0))
            return free_balance if free_balance > 0 else 1000.0 # Virtual $1000 if empty
        except Exception as e:
            print(f"[{_ts()}] ❌ Error fetching balance: {e}")
            return 1000.0

    def can_open_position(self, symbol):
        """Checks if we already have an open position for this symbol in Redis."""
        # For Paper Trading, we check our virtual storage instead of real wallet
        return not self.db.hexists('active_trades', symbol)

    def place_smart_order(self, symbol, amount_usdt=10, risk_percent=0.02):
        """
        Executes a market buy simulation and saves data for the Monitor.
        """
        try:
            print(f"[{_ts()}] 🛒 Executor: Processing SMART order for {symbol}")

            # 1. Check if we already have this trade active in our Monitor
            if not self.can_open_position(symbol):
                print(f"[{_ts()}] ⚠️ Already monitoring {symbol}. Skipping.")
                return {"status": "error", "msg": f"Already holding {symbol}"}

            # 2. Risk Management: Calculate position size
            total_usdt = self.get_free_usdt_balance()
            # Position size = 2% of total balance, but not less than 10 USDT
            calc_amount = max(total_usdt * risk_percent, 10)
            
            if calc_amount > total_usdt and total_usdt < 10:
                 return {"status": "error", "msg": "Insufficient funds"}
            
            # Use the calculated amount (but capped by what's available)
            final_amount_usdt = min(calc_amount, total_usdt)

            # 3. Load markets to get precision rules
            self.exchange.load_markets()
            
            # 4. Get current market price
            ticker = self.exchange.fetch_ticker(symbol)
            entry_price = ticker['last']
            
            # 5. Calculate target prices and quantity with precision
            tp_price = self.get_precision_price(symbol, entry_price * self.tp_percent)
            sl_price = self.get_precision_price(symbol, entry_price * self.sl_percent)
            
            raw_qty = final_amount_usdt / entry_price
            qty = self.get_precision_amount(symbol, raw_qty)

            # 6. Prepare the result object FIRST
            result = {
                "status": "success",
                "symbol": symbol,
                "entry": float(entry_price),
                "qty": float(qty),
                "tp": float(tp_price),
                "sl": float(sl_price),
                "timestamp": time.time()
            }

            print(f"[{_ts()}] 📊 Planned: Buy {qty} {symbol} @ {entry_price}")
            print(f"[{_ts()}] 🎯 Targets: TP: {tp_price} | SL: {sl_price}")

            # --- SIMULATION MODE ---
            # self.exchange.create_market_buy_order(symbol, qty)
            
            # 7. SAVE TO REDIS for Monitor to track (Crucial for MVP)
            self.db.hset('active_trades', symbol, json.dumps(result))
            
            # 8. Notify Messenger through Redis queue
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
            # Listen for 'trade_commands' queue (from Telegram/Brain)
            command_data = self.db.blpop('trade_commands', timeout=10)
            
            if command_data:
                _, payload = command_data
                try:
                    data = json.loads(payload)
                    symbol = data.get('symbol')
                    # We can pass custom amount if needed, otherwise uses Risk Management logic
                    amount = data.get('amount', 10)
                    
                    self.place_smart_order(symbol, amount)
                    
                except Exception as e:
                    print(f"[{_ts()}] ❌ Error parsing command: {e}")

if __name__ == "__main__":
    executor = Executor()
    executor.run()
