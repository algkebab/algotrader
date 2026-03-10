"""Brain service: strategy execution and GPT analysis."""
import asyncio
import json
import os
import sys
import time
from datetime import datetime

import redis
from dotenv import load_dotenv
from openai import OpenAI

# Allow importing shared.db (project root or /app in Docker)
_this_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.abspath(os.path.join(_this_dir, "..", "..")) if os.path.basename(_this_dir) == "brain" else _this_dir
if _root not in sys.path:
    sys.path.insert(0, _root)

from shared import db as shared_db

load_dotenv()

def _ts():
    return datetime.utcnow().strftime("%H:%M:%S")

# Redis key for max open orders (set by Messenger "orders set max"); default 10
REDIS_KEY_MAX_OPEN_ORDERS = "system:max_open_orders"
MAX_OPEN_ORDERS_DEFAULT = 10

# AI system prompts per strategy (set via Telegram "set strategy <name>")
STRATEGY_SYSTEM_MESSAGES = {
    "conservative": "You are an expert crypto day-trader. Be conservative and look for high-probability setups. Prefer WAIT unless the setup is very clear and well confirmed.",
    "moderate": "You are an expert crypto day-trader. Use a balanced approach: take setups with good risk/reward and allow more entries than conservative. Still require technical confirmation before BUY.",
    "aggressive": "You are an expert crypto day-trader. You are aggressive: take more BUY signals when technicals align. Higher risk tolerance. Favor BUY when RSI and volume support the move.",
    "active_day": "You are an expert crypto day-trader focused on active day-trading. Take frequent opportunities: scalp-style, more BUY signals on momentum and volume spikes. Quick in-and-out mindset.",
}

class Brain:
    def __init__(self):
        # Redis setup
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        
        # AI setup
        self.client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
        
        # Sensitivity setting: 0.005 = 0.5%
        self.price_change_threshold = 0.005

    def _get_strategy_system_content(self):
        """Read strategy from Redis (set by Messenger 'set strategy'); default conservative."""
        val = self.db.get("system:strategy")
        if not val:
            return STRATEGY_SYSTEM_MESSAGES["conservative"]
        key = str(val).strip().lower()
        return STRATEGY_SYSTEM_MESSAGES.get(key, STRATEGY_SYSTEM_MESSAGES["conservative"])

    def should_analyze(self, symbol, current_price):
        """
        Implements smart caching to save money.
        Returns True if price moved significantly or cache expired.
        """
        cache_data = self.db.get(f"cache:brain_price:{symbol}")
        
        if cache_data:
            last_price = float(cache_data)
            price_diff = abs(current_price - last_price) / last_price
            
            if price_diff < self.price_change_threshold:
                print(f"[{_ts()}] 🧠 Brain: Skipping {symbol} (Price change {price_diff:.2%} < {self.price_change_threshold:.2%})")
                return False
        
        # If no cache or price moved enough, we update and proceed
        self.db.set(f"cache:brain_price:{symbol}", current_price, ex=1800)
        return True

    def _get_max_open_orders(self):
        """Return max simultaneous open orders from Redis (default 10)."""
        val = self.db.get(REDIS_KEY_MAX_OPEN_ORDERS)
        if val is None or not str(val).isdigit():
            return MAX_OPEN_ORDERS_DEFAULT
        return max(1, min(50, int(val)))

    def _get_open_order_count(self):
        """Return number of open orders in DB."""
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                return len(shared_db.get_open_orders(conn))
        except Exception:
            return 0

    def get_ai_verdict(self, symbol, price, rsi, rvol, candles):
        """Sends technical data to GPT for a high-level trading verdict"""
        candle_summary = [f"Close: {c[4]}, Vol: {c[5]}" for c in candles[-5:]]
        
        prompt = f"""
        Analyze this crypto trade setup for {symbol}:
        - Current Price: {price}
        - RSI (14): {rsi}
        - Relative Volume (RVOL): {rvol}
        - Recent Price Action (Last 5h): {', '.join(candle_summary)}

        Provide a verdict in JSON format:
        {{
            "verdict": "BUY" or "WAIT",
            "reason": "Short technical explanation",
            "confidence": "0-100%"
        }}
        """

        system_content = self._get_strategy_system_content()
        try:
            response = self.client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": prompt}
                ],
                response_format={ "type": "json_object" }
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            print(f"[{_ts()}] ❌ AI Error: {e}")
            return {"verdict": "WAIT", "reason": "AI Analysis failed", "confidence": "0%"}

    def run(self):
        print(f"[{_ts()}] 🧠 Brain: AI Technical Analyst is online with Smart Cache...")
        PAUSED_KEY = "system:trading_paused"

        while True:
            if self.db.get(PAUSED_KEY):
                time.sleep(5)
                continue

            raw_data = self.db.get('filtered_candidates')
            if not raw_data:
                time.sleep(5)
                continue

            candidates = json.loads(raw_data)

            # Do not call OpenAI when at max open orders (no new orders would be placed)
            open_count = self._get_open_order_count()
            max_open = self._get_max_open_orders()
            if open_count >= max_open:
                print(f"[{_ts()}] 🧠 Brain: Skipping AI (max open orders reached: {open_count}/{max_open})")
                self.db.delete('filtered_candidates')
                time.sleep(5)
                continue

            for item in candidates:
                # Re-check at max capacity before each item (avoids race: 10th order opened after batch start)
                open_count = self._get_open_order_count()
                max_open = self._get_max_open_orders()
                if open_count >= max_open:
                    print(f"[{_ts()}] 🧠 Brain: Stopping (max open orders reached: {open_count}/{max_open})")
                    break

                symbol = item['symbol']
                current_price = item['last_price']

                # Skip if we already have an open order for this symbol
                try:
                    with shared_db.get_connection() as conn:
                        shared_db.init_schema(conn)
                        if shared_db.get_open_order_id_for_symbol(conn, symbol) is not None:
                            print(f"[{_ts()}] 🧠 Brain: Skipping {symbol} (already have open order)")
                            continue
                except Exception as e:
                    print(f"[{_ts()}] 🧠 Brain: DB check failed for {symbol}: {e}")
                    # Proceed with analysis if DB check fails
                
                # --- SMART CACHE START ---
                if not self.should_analyze(symbol, current_price):
                    continue
                # --- SMART CACHE END ---

                # Re-check again right before calling AI (no API call when at capacity)
                if self._get_open_order_count() >= self._get_max_open_orders():
                    print(f"[{_ts()}] 🧠 Brain: Skipping AI for {symbol} (max open orders reached)")
                    break

                print(f"[{_ts()}] 🔍 Analyzing {symbol} with AI...")
                
                analysis = self.get_ai_verdict(
                    symbol, 
                    current_price, 
                    item['rsi'], 
                    item['rvol'], 
                    item.get('candles', [])
                )

                # Merge AI verdict with market data
                final_signal = {**item, **analysis}

                # Never send a signal if at max open orders or already have open order for this symbol
                if self._get_open_order_count() >= self._get_max_open_orders():
                    print(f"[{_ts()}] 🧠 Brain: Not sending signal for {symbol} (max open orders reached)")
                    continue
                try:
                    with shared_db.get_connection() as conn:
                        shared_db.init_schema(conn)
                        if shared_db.get_open_order_id_for_symbol(conn, symbol) is not None:
                            print(f"[{_ts()}] 🧠 Brain: Not sending signal for {symbol} (open order exists)")
                            continue
                except Exception as e:
                    print(f"[{_ts()}] 🧠 Brain: DB check before push failed for {symbol}: {e}")
                    continue

                self.db.rpush('signals', json.dumps(final_signal))

            # Clear candidates after processing
            self.db.delete('filtered_candidates')
            time.sleep(5)

if __name__ == "__main__":
    brain = Brain()
    # Fixed: run is not an async function, using standard loop or calling directly
    brain.run()