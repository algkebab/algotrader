import redis
import json
import os
import ccxt
import time
from datetime import datetime

def _ts():
    return datetime.utcnow().strftime("%H:%M:%S")

class Monitor:
    def __init__(self):
        # Redis setup
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        
        # Exchange setup (for price tracking)
        self.exchange = ccxt.binance({
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })

    def run(self):
        print(f"[{_ts()}] 🛰️ Monitor: Tracking active positions...")
        
        while True:
            # 1. Get all active trades from Redis
            # We will store them in a hash map called 'active_trades'
            trades = self.db.hgetall('active_trades')
            
            if not trades:
                time.sleep(5) # Wait if no active trades
                continue

            for symbol, trade_json in trades.items():
                trade = json.loads(trade_json)
                
                try:
                    # 2. Fetch current price
                    ticker = self.exchange.fetch_ticker(symbol)
                    current_price = ticker['last']
                    
                    entry_price = trade['entry']
                    sl_price = trade['sl']
                    tp_price = trade['tp']
                    
                    print(f"[{_ts()}] 📊 Monitoring {symbol}: Now: {current_price} | SL: {sl_price} | TP: {tp_price}")

                    # 3. Check Stop-Loss
                    if current_price <= sl_price:
                        self.close_position(symbol, current_price, "STOP-LOSS 🔴")

                    # 4. Check Take-Profit
                    elif current_price >= tp_price:
                        # Instead of closing, we could implement Trailing Stop here
                        # For now, let's just close to lock in profit
                        self.close_position(symbol, current_price, "TAKE-PROFIT 🟢")
                    
                    # 5. Optional: Trailing Stop Logic
                    # If price is 2% above entry, move SL to entry price (Break even)
                    if current_price > entry_price * 1.02 and sl_price < entry_price:
                        trade['sl'] = entry_price
                        self.db.hset('active_trades', symbol, json.dumps(trade))
                        print(f"[{_ts()}] 🛡️ {symbol}: SL moved to BREAK-EVEN")

                except Exception as e:
                    print(f"[{_ts()}] ❌ Error monitoring {symbol}: {e}")

            time.sleep(2) # Price check frequency

    def close_position(self, symbol, price, reason):
        """Calculates final PnL and removes trade from active"""
        trade_json = self.db.hget('active_trades', symbol)
        if not trade_json:
            return
        
        trade = json.loads(trade_json)
        entry_price = float(trade['entry'])
        qty = float(trade['qty'])
        
        # PnL Calculation
        # Profit/Loss in USDT = (Current Price - Entry Price) * Quantity
        pnl_usdt = (price - entry_price) * qty
        # Profit/Loss in %
        pnl_percent = ((price / entry_price) - 1) * 100
        
        print(f"[{_ts()}] 🚩 CLOSING {symbol} at {price}. PnL: {pnl_usdt:.2f} USDT ({pnl_percent:.2f}%)")
        
        # Notify Messenger with detailed stats
        notification = {
            "type": "trade_closed",
            "data": {
                "symbol": symbol,
                "entry": entry_price,
                "exit": price,
                "pnl_usdt": round(pnl_usdt, 2),
                "pnl_percent": round(pnl_percent, 2),
                "reason": reason
            }
        }
        
        self.db.rpush('notifications', json.dumps(notification))
        self.db.hdel('active_trades', symbol)

if __name__ == "__main__":
    monitor = Monitor()
    monitor.run()