import json
import os
import sys
import time
import redis
import ccxt
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# Allow importing shared.db (project root or /app in Docker)
_this_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.abspath(os.path.join(_this_dir, "..", "..")) if os.path.basename(_this_dir) in ("executor", "monitor") else _this_dir
if _root not in sys.path:
    sys.path.insert(0, _root)

from shared import db as shared_db

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
        
        # 3. Активація Sandbox (Binance Spot Testnet)
        if os.getenv('IS_TESTNET', 'true').lower() == 'true':
            self.exchange.set_sandbox_mode(True)
            print(f"[{_ts()}] ⚠️ EXECUTOR: Running in BINANCE SPOT TESTNET mode")
        else:
            print(f"[{_ts()}] ⚡ EXECUTOR: Running in BINANCE REAL SPOT mode")
        
        # Default strategy settings
        self.tp_percent = 1.05  # +5%
        self.sl_percent = 0.98  # -2%
        # Paper trading only: simulated leverage (notional = margin * this)
        self.paper_leverage = 3

    def get_precision_amount(self, symbol, amount):
        """Adjusts the coin amount to the exchange's required precision."""
        self.exchange.load_markets()
        return float(self.exchange.amount_to_precision(symbol, amount))

    def get_precision_price(self, symbol, price):
        """Adjusts the price to the exchange's required precision."""
        self.exchange.load_markets()
        return float(self.exchange.price_to_precision(symbol, price))

    def get_free_usdt_balance(self):
        """Checks available USDT balance on Binance Spot account."""
        try:
            balance = self.exchange.fetch_balance()
            # На Binance використовуємо balance['free']
            return float(balance['free'].get('USDT', 0))
        except Exception as e:
            print(f"[{_ts()}] ❌ Error fetching balance: {e}")
            return 0.0

    def _is_paper_trading(self):
        """Paper trading is ON when key is absent or '1'; OFF when '0'."""
        val = self.db.get("system:papertrading")
        return val != "0"

    def can_open_position(self, symbol):
        """Checks if we already have an active trade for this symbol (Redis in live, DB in paper)."""
        if self._is_paper_trading():
            try:
                with shared_db.get_connection() as conn:
                    shared_db.init_schema(conn)
                    return shared_db.get_open_order_id_for_symbol(conn, symbol) is None
            except Exception:
                return True
        return not self.db.hexists('active_trades', symbol)

    def place_smart_order(self, symbol, amount_usdt=10, risk_percent=0.02):
        """
        Executes a market buy on Binance Spot (live) or writes order to DB only (paper).
        """
        try:
            paper = self._is_paper_trading()
            print(f"[{_ts()}] 🛒 Executor: Processing order for {symbol}" + (" (paper)" if paper else ""))

            if not self.can_open_position(symbol):
                print(f"[{_ts()}] ⚠️ Already monitoring {symbol}. Skipping.")
                return {"status": "error", "msg": "Position exists"}

            if paper:
                return self._place_paper_order(symbol, amount_usdt)

            # --- LIVE TRADING ---
            # 1. Risk Management
            total_usdt = self.get_free_usdt_balance()
            print(f"[{_ts()}] 💰 Current Balance: {total_usdt} USDT")
            
            calc_amount = max(total_usdt * risk_percent, 10.5) 
            if total_usdt < 10:
                 return {"status": "error", "msg": "Insufficient funds on Binance"}
            final_amount_usdt = min(calc_amount, total_usdt)

            # 2. Market Data
            ticker = self.exchange.fetch_ticker(symbol)
            entry_price = ticker['last']
            tp_price = self.get_precision_price(symbol, entry_price * self.tp_percent)
            sl_price = self.get_precision_price(symbol, entry_price * self.sl_percent)
            raw_qty = final_amount_usdt / entry_price
            qty = self.get_precision_amount(symbol, raw_qty)

            print(f"[{_ts()}] 🚀 SENDING MARKET BUY: {qty} {symbol}")
            order = self.exchange.create_market_buy_order(symbol, qty)
            actual_entry = order.get('average', entry_price)

            result = {
                "status": "success",
                "order_id": order.get('id'),
                "symbol": symbol,
                "entry": float(actual_entry),
                "qty": float(qty),
                "tp": float(tp_price),
                "sl": float(sl_price),
                "timestamp": time.time()
            }
            self.db.hset('active_trades', symbol, json.dumps(result))
            self.db.rpush('notifications', json.dumps({"type": "trade_confirmed", "data": result}))

            try:
                with shared_db.get_connection() as conn:
                    shared_db.init_schema(conn)
                    shared_db.insert_order(
                        conn, symbol=symbol, side="buy", amount_usdt=final_amount_usdt,
                        entry_price=float(actual_entry), quantity=float(qty),
                        tp_price=float(tp_price), sl_price=float(sl_price),
                        exchange_order_id=str(order["id"]) if order.get("id") else None,
                    )
                    shared_db.sync_balance_from_exchange(conn, self.exchange)
            except Exception as db_err:
                print(f"[{_ts()}] ⚠️ DB write failed (order still on exchange): {db_err}")

            print(f"[{_ts()}] ✅ Order confirmed on Binance! ID: {order.get('id')}")
            return result

        except Exception as e:
            print(f"[{_ts()}] ❌ Binance Order Error: {e}")
            return {"status": "error", "message": str(e)}

    def _place_paper_order(self, symbol, amount_usdt=10):
        """Write order to DB only; no exchange, no Redis active_trades. Uses paper_leverage (e.g. 3x) for notional."""
        try:
            # Public ticker for entry/tp/sl (no account interaction)
            ticker = self.exchange.fetch_ticker(symbol)
            entry_price = float(ticker['last'])
            # Same TP/SL proportions as live: +5% / -2% price levels
            tp_price = self.get_precision_price(symbol, entry_price * self.tp_percent)
            sl_price = self.get_precision_price(symbol, entry_price * self.sl_percent)
            # Paper leverage: notional = margin * leverage (e.g. 10 USDT margin -> 30 USDT position)
            effective_notional_usdt = amount_usdt * self.paper_leverage
            raw_qty = effective_notional_usdt / entry_price
            qty = self.get_precision_amount(symbol, raw_qty)
            final_amount_usdt = float(qty) * entry_price

            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                current_bal = shared_db.get_balance(conn, "USDT")
                if current_bal < amount_usdt:
                    print(f"[{_ts()}] ❌ Paper order skipped: insufficient balance ({current_bal:.2f} < {amount_usdt} USDT margin)")
                    return {"status": "error", "message": "Insufficient balance for paper order"}
                shared_db.set_balance(conn, "USDT", current_bal - amount_usdt)
                order_id = shared_db.insert_order(
                    conn,
                    symbol=symbol,
                    side="buy",
                    amount_usdt=final_amount_usdt,
                    entry_price=entry_price,
                    quantity=float(qty),
                    tp_price=float(tp_price),
                    sl_price=float(sl_price),
                    exchange_order_id=None,
                )
            result = {
                "status": "success",
                "order_id": f"paper-{order_id}",
                "symbol": symbol,
                "entry": entry_price,
                "qty": float(qty),
                "tp": float(tp_price),
                "sl": float(sl_price),
                "timestamp": time.time()
            }
            self.db.rpush('notifications', json.dumps({"type": "trade_confirmed", "data": result}))
            print(f"[{_ts()}] ✅ Paper order written to DB (id={order_id}, {self.paper_leverage}x leverage)")
            return result
        except Exception as e:
            print(f"[{_ts()}] ❌ Paper order error: {e}")
            return {"status": "error", "message": str(e)}

    def run(self):
        print(f"[{_ts()}] ⚡ Executor: Waiting for trade commands from Redis...")
        while True:
            command_data = self.db.blpop('trade_commands', timeout=10)
            if command_data:
                _, payload = command_data
                try:
                    data = json.loads(payload)
                    self.place_smart_order(data.get('symbol'), amount_usdt=float(data.get('amount', 10)))
                except Exception as e:
                    print(f"[{_ts()}] ❌ Parsing error: {e}")

if __name__ == "__main__":
    executor = Executor()
    executor.run()