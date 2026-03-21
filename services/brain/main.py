"""Brain service: strategy execution and GPT analysis."""
import json
import os
import sys
import time
import uuid

import redis
from dotenv import load_dotenv
from openai import OpenAI

WAIT_CACHE_TTL_RSI_LOW = 3600  # 60 min (RSI < 60)
WAIT_CACHE_TTL_RSI_MID = 1800  # 30 min (RSI 60-65)
WAIT_CACHE_TTL_RSI_HOT = 900   # 15 min (RSI > 65)
PRICE_SPIKE_BYPASS_PCT = 1.0   # 1% move bypasses cache

# Allow importing shared.db (project root or /app in Docker)
_this_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.abspath(os.path.join(_this_dir, "..", "..")) if os.path.basename(_this_dir) == "brain" else _this_dir
if _root not in sys.path:
    sys.path.insert(0, _root)

from shared import config as shared_config
from shared import db as shared_db
from shared import logger as shared_logger

load_dotenv()

log = shared_logger.get_logger("brain")

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

STRATEGY_DEFAULT = "CONSERVATIVE"


class Brain:
    def __init__(self):
        # Redis setup
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)

        # AI setup
        self.client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

    def _get_strategy_name(self):
        """Return current strategy name (default CONSERVATIVE)."""
        val = shared_db.get_setting_value(shared_config.SYSTEM_KEY_STRATEGY)
        name = (val or STRATEGY_DEFAULT).strip().upper()
        if name not in {"CONSERVATIVE", "AGGRESSIVE", "REVERSAL"}:
            name = STRATEGY_DEFAULT
        return name

    def should_analyze(self, symbol, current_price):
        """
        Implements smart caching to save money.
        Returns True if price moved significantly or cache expired.
        """
        # WAIT cache: negative cache keyed by symbol with last WAIT price
        wait_key = f"cache:brain_wait:{symbol}"
        wait_raw = self.db.get(wait_key)
        if wait_raw:
            try:
                payload = json.loads(wait_raw)
                last_wait_price = float(payload.get("price"))
            except (json.JSONDecodeError, TypeError, ValueError):
                last_wait_price = None

            if last_wait_price and last_wait_price > 0:
                price_change_pct = ((current_price - last_wait_price) / last_wait_price) * 100
                if price_change_pct > PRICE_SPIKE_BYPASS_PCT:
                    log.info(f"Brain: [Spike detected] {symbol} moved {price_change_pct:.2f}% since WAIT, bypassing cache")
                    return True

            log.info(f"Brain: Skipping {symbol} (recent WAIT verdict cache active)")
            return False

        cache_data = self.db.get(f"cache:brain_price:{symbol}")

        if cache_data:
            last_price = float(cache_data)
            price_diff = abs(current_price - last_price) / last_price

            if price_diff < shared_config.PRICE_CHANGE_THRESHOLD:
                log.info(f"Brain: Skipping {symbol} (Price change {price_diff:.2%} < {shared_config.PRICE_CHANGE_THRESHOLD:.2%})")
                return False

        # If no cache or price moved enough, we update and proceed
        self.db.set(f"cache:brain_price:{symbol}", current_price, ex=1800)
        return True

    def _wait_cache_ttl_seconds(self, rsi):
        """Return WAIT cache TTL in seconds based on RSI band."""
        try:
            if rsi is None:
                raise TypeError
            rsi_value = float(rsi)
        except (TypeError, ValueError):
            return WAIT_CACHE_TTL_RSI_MID

        if rsi_value < 60:
            return WAIT_CACHE_TTL_RSI_LOW
        if rsi_value <= 65:
            return WAIT_CACHE_TTL_RSI_MID
        return WAIT_CACHE_TTL_RSI_HOT

    def _get_max_open_orders(self):
        """Return max simultaneous open orders from DB (default 10)."""
        val = shared_db.get_setting_value(shared_config.SYSTEM_KEY_MAX_OPEN_ORDERS)
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

    def _get_performance_section(self) -> str:
        """Build a self-calibration block from the last 20 resolved BUY signals.

        Returns an empty string when there is not enough data (< 5 closed trades).
        """
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                stats = shared_db.get_recent_signal_win_rate(conn, limit=20)
        except Exception:
            return ""

        if stats["total"] < 5:
            return ""

        wr = stats["win_rate_pct"]
        avg_pnl = stats["avg_pnl_usdt"]
        total = stats["total"]
        wins = stats["wins"]

        if wr >= 60:
            directive = (
                "Win rate is healthy. Maintain your current selectivity — the strategy is working."
            )
        elif wr >= 45:
            directive = (
                "Win rate is slightly below target. Raise your bar modestly — be more demanding "
                "on volume confirmation and RSI positioning before issuing BUY."
            )
        else:
            directive = (
                "Win rate is critically low. You are entering too many losing trades. "
                "Be significantly more selective — prefer WAIT unless every criterion is clearly met. "
                "Avoid all borderline setups."
            )

        return (
            f"[Your Recent Performance — Last {total} closed BUY signals]\n"
            f"- Win Rate: {wr}% ({wins}/{total} profitable)\n"
            f"- Avg Net PnL: {avg_pnl:+.2f} USDT per trade\n"
            f"- Self-calibration directive: {directive}\n\n"
        )

    def get_ai_verdict(self, symbol, price, rsi, rvol, candles, high_24h, low_24h):
        """Sends technical data to GPT for a high-level trading verdict with TP/SL targets.

        Returns a tuple: (parsed_response_dict, signal_id).
        """
        signal_id = str(uuid.uuid4())

        recent_candles = candles[-5:] if candles else []
        candle_summary = [f"Close: {c[4]}, Vol: {c[5]}" for c in recent_candles]

        # 24h range context
        high_str = "N/A" if high_24h is None else f"{high_24h}"
        low_str = "N/A" if low_24h is None else f"{low_24h}"

        strategy = self._get_strategy_name()

        symbol_base = symbol.split("/")[0] if "/" in symbol else symbol
        is_major = symbol_base in {"BTC", "ETH"}

        performance_section = self._get_performance_section()

        prompt = f"""
{performance_section}Analyze this crypto trade setup for {symbol} in the context of the current market:
- Current Price: {price}
- 24h High: {high_str}
- 24h Low: {low_str}
- RSI (14): {rsi}
- Relative Volume (RVOL): {rvol}
- Recent Price Action (Last 5 candles): {', '.join(candle_summary) if candle_summary else 'N/A'}
- Active strategy from the pipeline: {strategy}

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
- For ALL symbols you MUST use stop_loss_pct >= 1.2 (never tighter than 1.2%).
- Place the stop loss below the recent local support or the lowest wick of the last 5 candles when possible.
- Avoid unrealistically tight stops that will be hit by normal intraday noise.

Rules for take_profit_pct:
- Choose a realistic take_profit_pct that gives a favorable risk/reward vs the stop loss, consistent with the active strategy {strategy}.

Behavior by strategy:
- When strategy is CONSERVATIVE, prefer WAIT unless the setup is very clean with strong confirmation and a clear invalidation level.
- When strategy is AGGRESSIVE, you may accept more marginal setups if momentum and volume are strong, but still respect the stop loss rules above.
- When strategy is REVERSAL, focus on oversold exhaustion, RSI below 30, rejection from lows, and bounces from support; avoid chasing late bounces that are far from the recent lows.

Respond with ONLY the JSON object, no comments or additional text.
"""

        strategy_key = self._get_strategy_name().lower()
        system_content = STRATEGY_SYSTEM_MESSAGES.get(strategy_key, STRATEGY_SYSTEM_MESSAGES["conservative"])
        stats = {
            "symbol": symbol,
            "price": price,
            "rsi": rsi,
            "rvol": rvol,
            "high_24h": high_24h,
            "low_24h": low_24h,
            "strategy": strategy,
            "recent_candles": recent_candles,
            "is_major": is_major,
        }
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
        except Exception as e:
            log.error(f"Brain: AI Error: {e}")
            data = {
                "verdict": "WAIT",
                "stop_loss_pct": 0.0,
                "take_profit_pct": 0.0,
                "reason": "AI Analysis failed",
                "confidence": "0%",
            }

        # Persist signal (stats we sent to AI, prompt, parsed response) with generated signal_id
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                shared_db.insert_signal(
                    conn,
                    signal_id=signal_id,
                    symbol=symbol,
                    stats=stats,
                    prompt=prompt,
                    response=data,
                )
        except Exception as e:
            log.error(f"Brain: Failed to persist signal {signal_id} for {symbol}: {e}")

        return data, signal_id

    def run(self):
        log.info("Brain: AI Technical Analyst is online with Smart Cache...")

        while True:
            if shared_db.get_setting_value(shared_config.SYSTEM_KEY_TRADING_PAUSED) == "1":
                time.sleep(5)
                continue

            raw_data = self.db.getset('filtered_candidates', json.dumps([]))
            if not raw_data:
                time.sleep(5)
                continue

            candidates = json.loads(raw_data)

            # Do not call OpenAI when at max open orders (no new orders would be placed)
            open_count = self._get_open_order_count()
            max_open = self._get_max_open_orders()
            if open_count >= max_open:
                log.info(f"Brain: Skipping AI (max open orders reached: {open_count}/{max_open})")
                self.db.delete('filtered_candidates')
                time.sleep(5)
                continue

            for item in candidates:
                # Re-check at max capacity before each item (avoids race: 10th order opened after batch start)
                open_count = self._get_open_order_count()
                max_open = self._get_max_open_orders()
                if open_count >= max_open:
                    log.info(f"Brain: Stopping (max open orders reached: {open_count}/{max_open})")
                    break

                symbol = item['symbol']
                current_price = item['last_price']

                # Skip if we already have an open order for this symbol
                try:
                    with shared_db.get_connection() as conn:
                        shared_db.init_schema(conn)
                        if shared_db.get_open_order_id_for_symbol(conn, symbol) is not None:
                            log.info(f"Brain: Skipping {symbol} (already have open order)")
                            continue
                except Exception as e:
                    log.error(f"Brain: DB check failed for {symbol}: {e}")
                    # Proceed with analysis if DB check fails

                # --- SMART CACHE START ---
                if not self.should_analyze(symbol, current_price):
                    continue
                # --- SMART CACHE END ---

                # Re-check again right before calling AI (no API call when at capacity)
                if self._get_open_order_count() >= self._get_max_open_orders():
                    log.info(f"Brain: Skipping AI for {symbol} (max open orders reached)")
                    break

                log.info(f"Brain: Analyzing {symbol} with AI...")

                analysis, signal_id = self.get_ai_verdict(
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
                    ttl = self._wait_cache_ttl_seconds(item.get("rsi"))
                    self.db.set(
                        wait_key,
                        json.dumps({"price": current_price}),
                        ex=ttl,
                    )
                    log.info(f"Brain: WAIT for {symbol}. Cache active for {ttl // 60} min.")

                # Merge AI verdict with market data and attach unique signal_id
                final_signal = {**item, **analysis, "signal_id": signal_id}

                # Never send a signal if at max open orders or already have open order for this symbol
                if self._get_open_order_count() >= self._get_max_open_orders():
                    log.info(f"Brain: Not sending signal for {symbol} (max open orders reached)")
                    continue
                try:
                    with shared_db.get_connection() as conn:
                        shared_db.init_schema(conn)
                        if shared_db.get_open_order_id_for_symbol(conn, symbol) is not None:
                            log.info(f"Brain: Not sending signal for {symbol} (open order exists)")
                            continue
                except Exception as e:
                    log.error(f"Brain: DB check before push failed for {symbol}: {e}")
                    continue

                self.db.rpush('signals', json.dumps(final_signal))

            time.sleep(5)


if __name__ == "__main__":
    brain = Brain()
    brain.run()
