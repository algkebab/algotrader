import redis
import json
import os
import ccxt
import time
from dotenv import load_dotenv

load_dotenv()

class Executor:
    def __init__(self):
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        
        self.exchange = ccxt.binance({
            'apiKey': os.getenv('BINANCE_API_KEY'),
            'secret': os.getenv('BINANCE_SECRET'),
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })

    def place_smart_order(self, symbol, amount_usdt=10):
        """Places a market buy and then sets TP/SL orders"""
        try:
            print(f"🛒 Executing SMART order for {symbol}...")
            
            # 1. Отримуємо поточну ціну (щоб знати, де ставити SL/TP)
            ticker = self.exchange.fetch_ticker(symbol)
            entry_price = ticker['last']
            
            # 2. Розраховуємо цілі
            tp_price = entry_price * 1.05  # +5%
            sl_price = entry_price * 0.98  # -2%

            # --- РЕАЛЬНА ТОРГІВЛЯ (закоментовано для тесту) ---
            # buy_order = self.exchange.create_market_buy_order(symbol, amount_usdt)
            # print(f"✅ Market Buy executed at {entry_price}")
            
            # В реальності тут треба розрахувати кількість монет (amount_usdt / entry_price)
            # sell_qty = buy_order['amount']
            
            # 3. Виставляємо лімітний ордер на продаж (Take Profit)
            # self.exchange.create_limit_sell_order(symbol, sell_qty, tp_price)
            
            # 4. Виставляємо стоп-лосс (це складніше, зазвичай через stop_loss_limit)
            # self.exchange.create_order(symbol, 'stop_loss_limit', 'sell', sell_qty, sl_price, {'stopPrice': sl_price})
            
            result = {
                "status": "success",
                "entry": entry_price,
                "tp": round(tp_price, 4),
                "sl": round(sl_price, 4),
                "msg": f"Bought {symbol} at {entry_price}. TP: {tp_price}, SL: {sl_price}"
            }
            return result

        except Exception as e:
            print(f"❌ Smart Order Error: {e}")
            return {"status": "error", "msg": str(e)}

    def run(self):
        print("⚡ Executor: High-speed trade monitoring active...")
        while True:
            command = self.db.blpop('trade_commands', timeout=10)
            if command:
                _, payload = command
                data = json.loads(payload)
                
                result = self.place_smart_order(data['symbol'], data['amount'])
                
                # Повертаємо результат у Messenger
                self.db.set(f"trade_result:{data['symbol']}", json.dumps(result), ex=60)
                # Також кинемо повідомлення в чергу для Telegram
                self.db.rpush('notifications', json.dumps({
                    "type": "trade_confirmed",
                    "data": result
                }))

if __name__ == "__main__":
    executor = Executor()
    executor.run()