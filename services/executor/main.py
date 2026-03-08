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
            'options': {'defaultType': 'spot'} # Працюємо тільки на споті
        })
        
        # 3. Активація Sandbox (для Testnet Bybit)
        if os.getenv('IS_TESTNET', 'true').lower() == 'true':
            self.exchange.set_sandbox_mode(True)
            print(f"[{_ts()}] ⚠️ EXECUTOR: Running in BYBIT TESTNET mode")
        
        # Default strategy settings
        self.tp_percent = 1.05  # +5%
        self.sl_percent = 0.98  # -2%

    def get_precision_amount(self, symbol, amount):
        return float(self.exchange.amount_to_precision(symbol, amount))

    def get_precision_price(self, symbol, price):
        return float(self.exchange.price_to_precision(symbol, price))

    def get_free_usdt_balance(self):
        """Checks available USDT balance on Bybit Spot."""
        try:
            balance = self.exchange.fetch_balance()
            # У Bybit Spot баланс лежить у 'total' або 'free'
            return float(balance['total'].get('USDT', 0))
        except Exception as e:
            print(f"[{_ts()}] ❌ Error fetching balance: {e}")
            return 0.0

    def can_open_position(self, symbol):
        """Checks if we already have an active trade for this symbol in Redis."""
        return not self.db.hexists('active_trades', symbol)

    def place_smart_order(self, symbol, amount_usdt=10, risk_percent=0.02):
        """
        Executes a real market buy on Bybit Spot and saves data for Monitor.
        """
        try:
            print(f"[{_ts()}] 🛒 Executor: Processing order for {symbol}")

            if not self.can_open_position(symbol):
                print(f"[{_ts()}] ⚠️ Already monitoring {symbol}. Skipping.")
                return {"status": "error", "msg": "Position exists"}

            # 1. Risk Management
            total_usdt = self.get_free_usdt_balance()
            print(f"[{_ts()}] 💰 Current Balance: {total_usdt} USDT")
            
            # Розрахунок суми (мінімум 10 USDT для біржі)
            calc_amount = max(total_usdt * risk_percent, 10.1) # 10.1 щоб бути впевненим
            
            if total_usdt < 10:
                 return {"status": "error", "msg": "Insufficient funds on Bybit"}
            
            final_amount_usdt = min(calc_amount, total_usdt)

            # 2. Market Data
            self.exchange.load_markets()
            ticker = self.exchange.fetch_ticker(symbol)
            entry_price = ticker['last']
            
            # 3. Precision Calculations
            tp_price = self.get_precision_price(symbol, entry_price * self.tp_percent)
            sl_price = self.get_precision_price(symbol, entry_price * self.sl_percent)
            
            raw_qty = final_amount_usdt / entry_price
            qty = self.get_precision_amount(symbol, raw_qty)

            # --- EXECUTION ON BYBIT ---
            print(f"[{_ts()}] 🚀 SENDING MARKET BUY: {qty} {symbol}")
            
            # Справжній ордер на біржу
            order = self.exchange.create_market_buy_order(symbol, qty)
            print(f"[{_ts()}] ✅ Order executed! ID: {order.get('id')}")

            # 4. Prepare Trade Object for Monitor
            result = {
                "status": "success",
                "order_id": order.get('id'),
                "symbol": symbol,
                "entry": float(entry_price),
                "qty": float(qty),
                "tp": float(tp_price),
                "sl": float(sl_price),
                "timestamp": time.time()
            }
            
            # 5. Save to Redis for Monitor (Monitor will now close the trade on Bybit)
            self.db.hset('active_trades', symbol, json.dumps(result))
            
            # 6. Notify Messenger
            self.db.rpush('notifications', json.dumps({
                "type": "trade_confirmed",
                "data": result
            }))
            
            return result

        except Exception as e:
            print(f"[{_ts()}] ❌ Bybit Order Error: {e}")
            return {"status": "error", "message": str(e)}

    def run(self):
        print(f"[{_ts()}] ⚡ Executor: Waiting for signals from Redis...")
        while True:
            command_data = self.db.blpop('trade_commands', timeout=10)
            if command_data:
                _, payload = command_data
                try:
                    data = json.loads(payload)
                    self.place_smart_order(data.get('symbol'))
                except Exception as e:
                    print(f"[{_ts()}] ❌ Parsing error: {e}")

if __name__ == "__main__":
    executor = Executor()
    executor.run()