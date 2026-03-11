"""Brain service: strategy execution and GPT analysis."""
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

import redis
from dotenv import load_dotenv
from openai import OpenAI

# Allow importing shared.db (project root or /app in Docker)
_this_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.abspath(os.path.join(_this_dir, "..", "..")) if os.path.basename(_this_dir) == "brain" else _this_dir
if _root not in sys.path:
    sys.path.insert(0, _root)

from shared import db as shared_db
from shared import config as shared_config

load_dotenv()

def _ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

# AI system prompts per AI strategy (set via Telegram "set strategy <name>")
STRATEGY_SYSTEM_MESSAGES = {
    "conservative": (
        "You are an expert crypto day-trader in CONSERVATIVE mode. "
        "Focus on high-probability setups only. Require strong volume confirmation, "
        "RSI below 70, and clean technical structure before considering BUY. "
        "Prefer WAIT unless the setup is very clear, with asymmetric risk/reward and "
        "a logical stop below recent support."
    ),
    "aggressive": (
        "You are an expert crypto day-trader in AGGRESSIVE mode. "
        "Focus on momentum and breakouts with higher risk tolerance. "
        "You may accept RSI up to 85 when strong volume and trend continuation justify it. "
        "Still avoid chasing obvious exhaustion wicks; look for continuation patterns, "
        "strong breakouts, and clear invalidation levels for the stop loss."
    ),
    "reversal": (
        "You are an expert crypto day-trader in REVERSAL mode. "
        "Focus on identifying oversold exhaustion and potential bounces from local bottoms. "
        "Look for RSI below 30, price-action rejection at or near the lows of the recent range, "
        "and signs of seller exhaustion (long lower wicks, volume spikes on bounces). "
        "Avoid late entries after the move has already bounced far from the lows."
    ),
}

FILTER_STRATEGY_DEFAULT = "CONSERVATIVE"

class Brain:
    def __init__(self):
        # Redis setup
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        
        # AI setup
        self.client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
        

    def _get_strategy_system_content(self):
        """Read strategy from Redis (set by Messenger 'set strategy'); default conservative."""
        val = self.db.get(shared_config.REDIS_KEY_STRATEGY)
        if not val:
            return STRATEGY_SYSTEM_MESSAGES["conservative"]
        key = str(val).strip().lower()
        return STRATEGY_SYSTEM_MESSAGES.get(key, STRATEGY_SYSTEM_MESSAGES["conservative"])

    def _get_filter_strategy_name(self):
        """Return current filter strategy name used by Filter service (default CONSERVATIVE)."""
        val = self.db.get(shared_config.REDIS_KEY_FILTER_STRATEGY)
        name = (val or FILTER_STRATEGY_DEFAULT).strip().upper()
        if name not in {"CONSERVATIVE", "AGGRESSIVE", "REVERSAL"}:
            name = FILTER_STRATEGY_DEFAULT
        return name

    def should_analyze(self, symbol, current_price):
        """
        Implements smart caching to save money.
        Returns True if price moved significantly or cache expired.
        """
        # Skip symbols that recently received a WAIT verdict (negative cache)
        wait_key = f"cache:brain_wait:{symbol}"
        if self.db.get(wait_key):
            print(f"[{_ts()}] 🧠 Brain: Skipping {symbol} (recent WAIT verdict cache active)")
            return False

        cache_data = self.db.get(f"cache:brain_price:{symbol}")
        
        if cache_data:
            last_price = float(cache_data)
            price_diff = abs(current_price - last_price) / last_price
            
            if price_diff < shared_config.PRICE_CHANGE_THRESHOLD:
                print(f"[{_ts()}] 🧠 Brain: Skipping {symbol} (Price change {price_diff:.2%} < {shared_config.PRICE_CHANGE_THRESHOLD:.2%})")
                return False
        
        # If no cache or price moved enough, we update and proceed
        self.db.set(f"cache:brain_price:{symbol}", current_price, ex=1800)
        return True

    def _get_max_open_orders(self):
        """Return max simultaneous open orders from Redis (default 10)."""
        val = self.db.get(shared_config.REDIS_KEY_MAX_OPEN_ORDERS)
        if val is None or not str(val).isdigit():
            return shared_config.MAX_OPEN_ORDERS_DEFAULT
        return max(1, min(50, int(val)))

    def _get_open_order_count(self):
        """Return number of open orders in DB."""
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                return len(shared_db.get_open_orders(conn))
        except Exception:
            return 0

    def get_ai_verdict(self, symbol, price, rsi, rvol, candles, high_24h, low_24h):
        """Sends technical data to GPT for a high-level trading verdict with TP/SL targets."""
        recent_candles = candles[-5:] if candles else []
        candle_summary = [f"Close: {c[4]}, Vol: {c[5]}" for c in recent_candles]

        # 24h range context
        high_str = "N/A" if high_24h is None else f"{high_24h}"
        low_str = "N/A" if low_24h is None else f"{low_24h}"

        filter_strategy = self._get_filter_strategy_name()

        symbol_base = symbol.split("/")[0] if "/" in symbol else symbol
        is_major = symbol_base in {"BTC", "ETH"}

        prompt = f"""
Analyze this crypto trade setup for {symbol} in the context of the current market:
- Current Price: {price}
- 24h High: {high_str}
- 24h Low: {low_str}
- RSI (14): {rsi}
- Relative Volume (RVOL): {rvol}
- Recent Price Action (Last 5 candles): {', '.join(candle_summary) if candle_summary else 'N/A'}
- Active filter strategy from the pipeline: {filter_strategy}

You must act as a strategic architect for entries and exits, not just a simple yes/no filter.

Return a STRICT JSON object with this exact schema:
{{
  "verdict": "BUY" or "WAIT",
  "stop_loss_pct": "Float (e.g., 1.5 for 1.5% below entry)",
  "take_profit_pct": "Float (e.g., 4.5 for 4.5% above entry)",
  "reason": "Technical justification for these specific levels and the verdict",
  "confidence": "0-100%"
}}

Rules for stop_loss_pct (very important):
- stop_loss_pct is the percentage distance BELOW the entry price where the stop should be placed.
- For BTC/USDT and ETH/USDT you MUST use stop_loss_pct >= 0.8 (never tighter than 0.8%).
- For ALL other symbols you MUST use stop_loss_pct >= 1.2 (never tighter than 1.2%).
- Place the stop loss below the recent local support or the lowest wick of the last 5 candles when possible.
- Avoid unrealistically tight stops that will be hit by normal intraday noise.

Rules for take_profit_pct:
- Choose a realistic take_profit_pct that gives a favorable risk/reward vs the stop loss, consistent with the active filter strategy {filter_strategy}.

Behavior by strategy:
- When filter strategy is CONSERVATIVE, prefer WAIT unless the setup is very clean with strong confirmation and a clear invalidation level.
- When filter strategy is AGGRESSIVE, you may accept more marginal setups if momentum and volume are strong, but still respect the stop loss rules above.
- When filter strategy is REVERSAL, focus on oversold exhaustion, RSI below 30, rejection from lows, and bounces from support; avoid chasing late bounces that are far from the recent lows.

Respond with ONLY the JSON object, no comments or additional text.
"""

        system_content = self._get_strategy_system_content()
        try:
            response = self.client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            data = json.loads(content)
            return data
        except Exception as e:
            print(f"[{_ts()}] ❌ AI Error: {e}")
            return {
                "verdict": "WAIT",
                "stop_loss_pct": 0.0,
                "take_profit_pct": 0.0,
                "reason": "AI Analysis failed",
                "confidence": "0%",
            }

    def run(self):
        print(f"[{_ts()}] 🧠 Brain: AI Technical Analyst is online with Smart Cache...")
        PAUSED_KEY = shared_config.REDIS_KEY_TRADING_PAUSED

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
                    item.get('candles', []),
                    item.get('high_24h'),
                    item.get('low_24h'),
                )

                # Negative cache: when AI says WAIT, cache symbol to avoid re-analyzing flat charts
                if str(analysis.get("verdict", "")).upper() == "WAIT":
                    wait_key = f"cache:brain_wait:{symbol}"
                    self.db.set(wait_key, "1", ex=1800)

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