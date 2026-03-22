"""Brain service: strategy execution and GPT analysis."""
import json
import math
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
from shared import decision as shared_decision
from shared import logger as shared_logger
from shared.version import BOT_VERSION

load_dotenv()

log = shared_logger.get_logger("brain")

# AI system prompts per strategy — each defines a concrete decision framework,
# not just a persona. The AI is told HOW to reason, not just who it is.
STRATEGY_SYSTEM_MESSAGES = {
    "conservative": """You are a professional crypto trader with 10+ years of experience, \
trained at a quantitative prop desk. You are currently operating in CONSERVATIVE mode \
where capital preservation is the primary objective.

Your decision framework (apply in order):
1. Higher-timeframe bias first: the 1h trend must be BULLISH or NEUTRAL. \
   A bearish 1h trend is an immediate disqualifier — do not fight it.
2. Structure before indicators: read the 1h and 15m candle sequence for higher highs / \
   higher lows (uptrend) vs lower highs / lower lows (downtrend) vs consolidation. \
   Indicators confirm what price structure shows, they do not replace it.
3. Confluence minimum: require at least 3 of these 5 signals aligned bullishly before BUY — \
   (a) price above VWAP, (b) 15m EMA stack bullish, (c) MACD histogram positive and growing, \
   (d) RSI 45–65 (momentum without overbought), (e) RVOL ≥ 2.0 confirming participation.
4. Volume context: a price move on below-average volume is a fakeout. \
   Volume must expand on the breakout candle or the move preceding entry.
5. Risk definition before profit: identify the exact stop level (below last key low or \
   lowest wick in recent 15m range) BEFORE evaluating the TP. \
   If you cannot find a logical stop, the trade does not exist.
6. Minimum R:R 2.5:1. If the nearest visible resistance does not give 2.5× the SL distance, WAIT.
7. When in doubt, WAIT. A missed trade costs nothing. A bad trade costs real money.""",

    "aggressive": """You are a professional crypto trader with 10+ years of experience, \
specializing in momentum breakouts and trend continuation. \
You are currently operating in AGGRESSIVE mode.

Your decision framework (apply in order):
1. Momentum identification: you are looking for breakouts with strong volume expansion \
   and EMA trend alignment, not overextended chases. Momentum must be fresh.
2. Timeframe alignment: 15m EMA stack bullish is required. \
   1h neutral is acceptable; 1h bullish is ideal. 1h bearish = only take if 15m momentum is \
   exceptionally strong with RVOL ≥ 3.0.
3. Volume confirmation: RVOL ≥ 1.5 minimum. On breakout setups, volume must expand \
   above the average of the last 5 candles.
4. Momentum freshness: MACD histogram must be positive. If histogram is positive but shrinking \
   for 2+ bars, momentum is fading — consider WAIT.
5. Entry quality: price breaking above a clear resistance level with body close above it \
   is an A-grade entry. Price in the middle of a range with no clear trigger is B-grade or lower.
6. Risk definition: always identify a specific stop level. RSI up to 85 is acceptable \
   but the stop must still sit below a structural low, not be set arbitrarily.
7. Minimum R:R 2.0:1. Accept 1.8:1 only if momentum is exceptionally strong (RVOL > 3, \
   strong MACD, multiple timeframe alignment).""",

    "reversal": """You are a professional crypto trader with 10+ years of experience, \
specializing in identifying exhaustion and capitulation at market lows. \
You are currently operating in REVERSAL mode — you are a counter-trend hunter.

Your decision framework (apply in order):
1. Oversold exhaustion is required: RSI must be below 30 on the 15m. \
   RSI 30–40 means the bounce has already started — too late, WAIT for the next setup.
2. Price location: price must be at or near the lower Bollinger Band (%B ≤ 25%). \
   A reversal entry 10%+ above the recent swing low is chasing, not reversing.
3. Seller exhaustion signals — look for ALL of these in the 15m candles: \
   (a) long lower wicks (rejection), (b) decreasing sell volume on recent down bars, \
   (c) MACD histogram bearish but magnitude shrinking (momentum exhausting). \
   Two out of three is minimum. All three is ideal.
4. Candle confirmation: a bullish engulfing, hammer, or doji with lower wick \
   on increasing volume is the entry trigger. Do not enter on a still-falling candle.
5. 1h trend context: 1h will be bearish — that is expected. \
   But the 1h MACD histogram should be slowing (bars getting smaller), not accelerating.
6. Wider stops accepted: stops must go below the most recent swing low. \
   This is often 2–4% — this is correct for reversal trades.
7. Minimum R:R 3.0:1 — counter-trend trades have lower win rate and must compensate with \
   larger reward. If you cannot find a 3:1 target in the candle structure, WAIT.""",
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

    def _get_decision_mode(self) -> str:
        """Return 'gpt' or 'code' (default: gpt)."""
        val = shared_db.get_setting_value(shared_config.SYSTEM_KEY_DECISION_MODE)
        if val and val.lower() == "code":
            return "code"
        return "gpt"

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

    def _get_btc_bias(self, btc_ctx: dict) -> str:
        """Return only the bias label from BTC context without building the formatted text."""
        ema_1h = (btc_ctx.get("ema_stack_1h") or {})
        ema_15m = (btc_ctx.get("ema_stack_15m") or {})
        macd = (btc_ctx.get("macd_15m") or {})
        rsi = btc_ctx.get("rsi")
        ema_1h_align = ema_1h.get("alignment", "MIXED")
        ema_15m_align = ema_15m.get("alignment", "MIXED")
        macd_bullish = (macd.get("histogram") or 0) > 0
        rsi_val = float(rsi) if rsi is not None else 50.0
        bearish = {"BEARISH", "WEAKENING"}
        bullish = {"BULLISH", "RECOVERING"}
        if ema_1h_align in bearish and ema_15m_align in bearish and rsi_val < 40:
            return "STRONG_BEARISH"
        if ema_1h_align in bearish and not macd_bullish:
            return "BEARISH_HEADWIND"
        if ema_1h_align in bullish and macd_bullish:
            return "BULLISH_TAILWIND"
        return "NEUTRAL"

    def _get_btc_context(self) -> dict | None:
        """Read BTC market context from Redis (published by Filter each cycle).

        Returns None when the key is missing or stale (TTL expired).
        """
        raw = self.db.get(shared_config.REDIS_KEY_BTC_CONTEXT)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    def _get_market_regime(self) -> dict | None:
        """Read market regime from Redis (published by Filter after each scan cycle)."""
        raw = self.db.get(shared_config.REDIS_KEY_MARKET_REGIME)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    def _format_btc_context_for_prompt(self, btc_ctx: dict) -> tuple[str, str]:
        """Format BTC context as a natural-language block and derive the market bias label.

        Returns (formatted_section: str, bias_label: str).
        bias_label is one of: BULLISH_TAILWIND, NEUTRAL, BEARISH_HEADWIND, STRONG_BEARISH.
        """
        lines = []

        price = btc_ctx.get("price")
        change_24h = btc_ctx.get("change_24h")
        rsi = btc_ctx.get("rsi")
        vwap_pct = btc_ctx.get("vwap_pct")
        ema_15m = btc_ctx.get("ema_stack_15m") or {}
        ema_1h = btc_ctx.get("ema_stack_1h") or {}
        macd = btc_ctx.get("macd_15m") or {}

        if price is not None:
            change_str = f" ({change_24h:+.2f}% 24h)" if change_24h is not None else ""
            lines.append(f"- BTC Price: ${float(price):,.2f}{change_str}")

        if rsi is not None:
            lines.append(f"- BTC RSI (15m): {rsi}")

        if vwap_pct is not None:
            direction = "above" if vwap_pct > 0 else "below"
            lines.append(f"- BTC vs VWAP: {abs(vwap_pct):.2f}% {direction} VWAP")

        if ema_15m.get("description"):
            lines.append(f"- BTC EMA Stack (15m): {ema_15m['description']}")

        if ema_1h.get("description"):
            lines.append(f"- BTC EMA Stack (1h): {ema_1h['description']}")

        if macd.get("histogram") is not None:
            h = macd["histogram"]
            h_prev = macd.get("histogram_prev")
            sign = "bullish" if h > 0 else "bearish"
            momentum = ""
            if h_prev is not None:
                momentum = ", strengthening" if abs(h) > abs(h_prev) else ", weakening"
            lines.append(f"- BTC MACD (15m): histogram={h:+.6g} ({sign}{momentum})")

        # Derive market bias from 1h EMA alignment + MACD + RSI
        ema_1h_align = ema_1h.get("alignment", "MIXED")
        ema_15m_align = ema_15m.get("alignment", "MIXED")
        macd_bullish = (macd.get("histogram") or 0) > 0
        rsi_value = float(rsi) if rsi is not None else 50.0

        bearish_alignments = {"BEARISH", "WEAKENING"}
        bullish_alignments = {"BULLISH", "RECOVERING"}

        if ema_1h_align in bearish_alignments and ema_15m_align in bearish_alignments and rsi_value < 40:
            bias = "STRONG_BEARISH"
            bias_label = "STRONG BEARISH — avoid all altcoin longs"
        elif ema_1h_align in bearish_alignments and not macd_bullish:
            bias = "BEARISH_HEADWIND"
            bias_label = "BEARISH HEADWIND — significantly raise the entry bar"
        elif ema_1h_align in bullish_alignments and macd_bullish:
            bias = "BULLISH_TAILWIND"
            bias_label = "BULLISH TAILWIND — standard entry criteria apply"
        else:
            bias = "NEUTRAL"
            bias_label = "NEUTRAL — evaluate altcoin on own merits, raise selectivity slightly"

        lines.append(f"→ Market Bias: {bias_label}")
        return "\n".join(lines), bias

    def _format_indicators_for_prompt(self, price: float, indicators: dict) -> str:
        """Format computed technical indicators as natural-language lines for the AI prompt."""
        lines = []

        # VWAP — institutional price benchmark
        vwap = indicators.get("vwap")
        if vwap and price:
            pct = (price - vwap) / vwap * 100
            direction = "above" if pct > 0 else "below"
            lines.append(f"- VWAP: {vwap:.6g} — price is {abs(pct):.2f}% {direction} VWAP")

        # ATR — volatility-adjusted stop guide
        atr = indicators.get("atr")
        if atr and price:
            atr_pct = atr / price * 100
            suggested_sl_pct = round(atr_pct * 1.5, 2)
            lines.append(
                f"- ATR (14, 15m): {atr:.6g} ({atr_pct:.2f}% of price) "
                f"→ 1.5× ATR suggested SL = {suggested_sl_pct:.2f}% below entry"
            )

        # EMA stack (15m) — short-term entry trend
        ema_15m = indicators.get("ema_stack_15m")
        if ema_15m:
            lines.append(f"- EMA Stack (15m): {ema_15m['description']}")

        # EMA stack (1h) — higher-timeframe trend bias
        ema_1h = indicators.get("ema_stack_1h")
        if ema_1h:
            lines.append(f"- EMA Stack (1h, trend bias): {ema_1h['description']}")

        # EMA stack (4h) — dominant trend gate (days-level direction)
        ema_4h = indicators.get("ema_stack_4h")
        if ema_4h:
            lines.append(f"- EMA Stack (4h, trend gate): {ema_4h['description']}")

        # Bollinger Bands — volatility squeeze and price position
        bb = indicators.get("bollinger_15m")
        if bb:
            pct_b = bb["pct_b"]
            bw = bb["bandwidth"]
            if pct_b > 80:
                bb_zone = "near upper band — overbought or breakout continuation"
            elif pct_b < 20:
                bb_zone = "near lower band — oversold or breakdown continuation"
            else:
                bb_zone = "mid-range"
            squeeze = (
                "squeeze — breakout likely soon" if bw < 2.0
                else ("expanding" if bw > 5.0 else "normal")
            )
            lines.append(
                f"- Bollinger Bands (15m): %B={pct_b:.0f}% ({bb_zone}), "
                f"bandwidth={bw:.1f}% ({squeeze})"
            )

        # MACD histogram — momentum direction
        macd = indicators.get("macd_15m")
        if macd:
            h = macd["histogram"]
            h_prev = macd.get("histogram_prev")
            sign = "bullish" if h > 0 else "bearish"
            momentum = ""
            if h_prev is not None:
                momentum = ", strengthening" if abs(h) > abs(h_prev) else ", weakening"
            lines.append(f"- MACD (15m): histogram={h:+.6g} ({sign}{momentum})")

        return "\n".join(lines) if lines else "  N/A (indicators unavailable)"

    def get_ai_verdict(self, symbol, price, rsi, rvol, candles_15m, candles_1h,
                       high_24h, low_24h, indicators=None, change_24h=None, volume_24h=None):
        """Send enriched technical data to GPT for a trading verdict with TP/SL targets.

        Returns a tuple: (parsed_response_dict, signal_id).
        """
        signal_id = str(uuid.uuid4())
        indicators = indicators or {}

        # Recent 15m candles for entry price action context (last 30 = ~7.5 hours)
        recent_15m = candles_15m[-30:] if candles_15m else []
        candle_15m_lines = [
            f"  O:{c[1]:.6g} H:{c[2]:.6g} L:{c[3]:.6g} C:{c[4]:.6g} V:{c[5]:.0f}"
            for c in recent_15m
        ]

        # Recent 1h candles for trend context (last 24 = 24 hours, full OHLCV for structure)
        recent_1h = candles_1h[-24:] if candles_1h else []
        candle_1h_lines = [
            f"  O:{c[1]:.6g} H:{c[2]:.6g} L:{c[3]:.6g} C:{c[4]:.6g} V:{c[5]:.0f}"
            for c in recent_1h
        ]

        # 24h range
        high_str = "N/A" if high_24h is None else f"{high_24h}"
        low_str = "N/A" if low_24h is None else f"{low_24h}"

        strategy = self._get_strategy_name()
        symbol_base = symbol.split("/")[0] if "/" in symbol else symbol
        is_major = symbol_base in {"BTC", "ETH"}

        performance_section = self._get_performance_section()
        indicators_section = self._format_indicators_for_prompt(price, indicators)

        # BTC macro context — only relevant for non-BTC symbols
        btc_section = ""
        btc_bias = "NEUTRAL"
        if symbol_base != "BTC":
            btc_ctx = self._get_btc_context()
            if btc_ctx:
                btc_section_text, btc_bias = self._format_btc_context_for_prompt(btc_ctx)
                btc_section = f"\n### BTC Market Context (macro barometer)\n{btc_section_text}\n"

        # Market regime context
        regime_ctx = self._get_market_regime()
        regime_section = ""
        if regime_ctx:
            regime_name = regime_ctx.get('regime', 'MIXED')
            regime_guidance = {
                'BULL_TRENDING': (
                    'Momentum and breakout strategies are favored. '
                    'Full position sizing applies. Standard entry criteria.'
                ),
                'BEAR_TRENDING': (
                    'Only the highest-quality reversal setups pass. '
                    'Raise selectivity dramatically. Reduce TP to nearest visible resistance. '
                    'Any momentum or breakout setup should be WAIT.'
                ),
                'RANGING': (
                    'Market is non-trending. Breakout entries are fakeout-prone — avoid them. '
                    'Mean-reversion setups to range boundaries are preferred. '
                    'Tighter TP targets at the range ceiling.'
                ),
                'MIXED': (
                    'Uncertain regime. Raise selectivity by one level — '
                    'prefer A-grade setups only, flag any uncertainty in reason.'
                ),
            }.get(regime_name, 'Apply standard criteria.')
            regime_section = (
                f"\n### Market Regime (system-level classification)\n"
                f"- Regime: **{regime_name}** (confidence: {regime_ctx.get('confidence', '?')}%)\n"
                f"- Market Breadth: {regime_ctx.get('breadth_bullish_pct', '?')}% of tracked assets "
                f"in bullish 4h EMA structure\n"
                f"- Trend Strength ADX(4h): {regime_ctx.get('adx_4h') or 'N/A'} "
                f"(>25 = trending, <20 = ranging)\n"
                f"- Volatility: {regime_ctx.get('vol_regime', 'N/A')} "
                f"(realized vol ratio vs 50-bar baseline: {regime_ctx.get('atr_ratio', 'N/A')}×)\n"
                f"- Position Sizing: {regime_ctx.get('position_size_multiplier', 1.0)}× normal\n"
                f"→ Regime directive: {regime_guidance}\n"
            )

        # ATR-based SL hint for the rules section
        atr = indicators.get("atr")
        atr_sl_hint = ""
        if atr and price:
            atr_pct = atr / price * 100
            atr_sl_pct = round(atr_pct * 1.5, 2)
            atr_sl_hint = (
                f" The ATR-based guide for this symbol is {atr_sl_pct:.2f}% (1.5× ATR)."
                f" Use this as your primary SL sizing reference when it exceeds 1.2%."
            )

        prompt = f"""
{performance_section}## Market Data: {symbol}
Active Strategy: {strategy}
{btc_section}{regime_section}
### Price Context
- Current Price: {price}
- 24h High: {high_str} | 24h Low: {low_str}
- RSI (14, 15m): {rsi}
- Relative Volume (RVOL): {rvol}x average

### Computed Indicators
{indicators_section}

### 15m Candles — last {len(recent_15m)} bars [timestamp, O, H, L, C, Vol]
(Entry structure — read for higher highs/lows, candle patterns, breakout levels)
{chr(10).join(candle_15m_lines) if candle_15m_lines else "  N/A"}

### 1h Candles — last {len(recent_1h)} bars [timestamp, O, H, L, C, Vol]
(Trend structure — read for dominant trend direction and key swing levels)
{chr(10).join(candle_1h_lines) if candle_1h_lines else "  N/A"}

---
## Your Analysis Task

Work through the following steps in your response. Each field in the JSON is a reasoning step — \
fill them sequentially, as each step informs the next.

**Step 1 — 1h Trend Structure:**
Read the 1h candle sequence. Is price making higher highs and higher lows (uptrend), \
lower highs and lower lows (downtrend), or ranging between levels (consolidation)? \
What are the key swing high and swing low visible in the 1h data?

**Step 2 — 15m Setup:**
Read the 15m candle sequence. What pattern is forming — breakout above resistance, \
pullback to support, range compression, or exhaustion? \
Identify any significant candlestick patterns (hammer, engulfing, doji, shooting star, \
inside bar). What is the most recent key low that defines the stop?

**Step 3 — Volume Context:**
Is RVOL above or below normal? Does the volume action confirm the price move \
(expansion on directional bars, contraction on pullbacks) or contradict it \
(price rising on declining volume = weak)? Is there institutional participation?

**Step 4 — Indicator Confluence:**
List every signal that argues FOR a long trade. \
Then list every signal that argues AGAINST it. \
Count the confluence. Be honest about contradictions — do not ignore them.

**Step 5 — Risk Definition (stop first, profit second):**
Identify the exact structural stop level from the candle data \
(below the last key low or lowest wick in the 15m range). \
Convert to a percentage below current price. \
Then identify the nearest meaningful resistance from the candle data as the TP target. \
Calculate the actual R:R ratio.{atr_sl_hint}

**Step 6 — Timeframe Conflict Rule:**
If the 15m setup is bullish but the 1h trend is bearish: \
CONSERVATIVE = WAIT; AGGRESSIVE = only if 15m momentum is extremely strong (RVOL > 3); \
REVERSAL = expected, assess exhaustion quality instead.

**Step 7 — BTC Macro Correlation:**
BTC drives 60–85% of altcoin price movement. Current BTC bias: {btc_bias}.
Apply these rules strictly:
- BULLISH_TAILWIND: standard entry criteria from your strategy apply.
- NEUTRAL: raise selectivity by one level — prefer A-grade setups, flag uncertainty in reason.
- BEARISH_HEADWIND: require A-grade setup minimum; reduce TP to nearest visible resistance \
  (do not reach for extended targets); lean towards WAIT on any marginal setup.

**Step 8 — Final Verdict:**
Apply the {strategy} strategy criteria from your system instructions \
(including the BTC macro rule above). \
Is this an A-grade setup (3+ strong confluence signals, clear structure, logical SL) \
or B-grade (mixed signals, weak volume, unclear structure)? \
CONSERVATIVE and REVERSAL require A-grade. AGGRESSIVE may accept B-grade with strong momentum.

---
Return your full analysis as a single JSON object with this exact schema \
(no extra keys, no comments):
{{
  "trend_1h": "BULLISH | BEARISH | NEUTRAL — describe the HH/HL or LH/LL structure seen",
  "setup_15m": "BREAKOUT | PULLBACK | REVERSAL | CONSOLIDATION | NONE — describe the specific pattern",
  "candle_patterns": "Any significant patterns identified (e.g. hammer at support, bearish engulfing) or NONE",
  "volume_verdict": "CONFIRMING | NEUTRAL | CONTRADICTING — one sentence on volume-price relationship",
  "confluence_signals": ["list each signal that supports a long entry"],
  "conflicting_signals": ["list each signal that argues against a long entry"],
  "setup_grade": "A | B | C",
  "verdict": "BUY | WAIT",
  "stop_loss_pct": 0.0,
  "take_profit_pct": 0.0,
  "rr_ratio": 0.0,
  "confidence": 0,
  "reason": "2-3 sentence synthesis: trend context + entry trigger + what confirms + what invalidates"
}}

Rules (non-negotiable):
- stop_loss_pct: percentage BELOW entry. Minimum 1.2%. Must be anchored to a structural level.
- take_profit_pct: percentage ABOVE entry. Anchored to visible resistance in the candle data.
- rr_ratio: take_profit_pct / stop_loss_pct. Must meet your strategy's minimum R:R.
- confidence: integer 0–100, defined as: (number of confluence_signals / 6) × 100, \
  capped at 95. Do not invent a number — calculate it.
- If R:R does not meet the strategy minimum, verdict must be WAIT regardless of other signals.

Respond with ONLY the JSON object.
"""

        strategy_key = self._get_strategy_name().lower()
        system_content = STRATEGY_SYSTEM_MESSAGES.get(strategy_key, STRATEGY_SYSTEM_MESSAGES["conservative"])
        # Flatten key indicator values as top-level fields for ML training queries.
        # Nested dicts (ema_stack, bollinger, macd) are kept in indicators but critical
        # scalar values are promoted here so they're queryable without JSON parsing.
        ema_stack_15m = indicators.get("ema_stack_15m") or {}
        ema_stack_1h = indicators.get("ema_stack_1h") or {}
        ema_stack_4h = indicators.get("ema_stack_4h") or {}
        macd_15m = indicators.get("macd_15m") or {}
        bollinger_15m = indicators.get("bollinger_15m") or {}
        stats = {
            "symbol": symbol,
            "price": price,
            "rsi": rsi,
            "rvol": rvol,
            "change_24h": change_24h,
            "volume_24h": volume_24h,
            "high_24h": high_24h,
            "low_24h": low_24h,
            "strategy": strategy,
            "btc_bias": btc_bias,
            "candles_15m_count": len(candles_15m),
            "candles_1h_count": len(candles_1h),
            "is_major": is_major,
            "bot_version": BOT_VERSION,
            # Flat indicator scalars for ML
            "atr_at_entry": indicators.get("atr"),
            "ema_alignment_15m": ema_stack_15m.get("alignment"),
            "ema_alignment_1h": ema_stack_1h.get("alignment"),
            "ema_alignment_4h": ema_stack_4h.get("alignment"),
            "macd_hist_15m": macd_15m.get("histogram"),
            "bb_pct_b_15m": bollinger_15m.get("pct_b"),
            # Market regime at time of signal
            "market_regime": regime_ctx.get("regime") if regime_ctx else None,
            "breadth_bullish_pct": regime_ctx.get("breadth_bullish_pct") if regime_ctx else None,
            "vol_regime": regime_ctx.get("vol_regime") if regime_ctx else None,
            # Full indicator dicts for completeness
            "indicators": indicators,
        }
        last_err = None
        data = None
        for attempt in range(2):
            try:
                response = self.client.chat.completions.create(
                    model="gpt-4o",
                    temperature=0,  # deterministic — same input always produces same verdict
                    messages=[
                        {"role": "system", "content": system_content},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"},
                    timeout=30,
                )
                content = response.choices[0].message.content
                data = json.loads(content)
                last_err = None
                break
            except Exception as e:
                last_err = e
                if attempt == 0:
                    log.warning(f"Brain: AI call failed (attempt 1), retrying in 3s: {e}")
                    time.sleep(3)
        if last_err is not None:
            log.error(f"Brain: AI Error after retry: {last_err}")
            data = {
                "trend_1h": "UNKNOWN",
                "setup_15m": "NONE",
                "candle_patterns": "NONE",
                "volume_verdict": "NEUTRAL",
                "confluence_signals": [],
                "conflicting_signals": [],
                "setup_grade": "C",
                "verdict": "WAIT",
                "stop_loss_pct": 0.0,
                "take_profit_pct": 0.0,
                "rr_ratio": 0.0,
                "confidence": 0,
                "reason": "AI analysis failed",
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
        log.info(f"Brain: AI Technical Analyst is online with Smart Cache... [version: {BOT_VERSION}]")
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                shared_db.set_setting(conn, "bot_version", BOT_VERSION)
        except Exception as e:
            log.warning(f"Brain: Could not write bot_version to DB: {e}")

        while True:
            if shared_db.get_setting_value(shared_config.SYSTEM_KEY_TRADING_PAUSED) == "1":
                time.sleep(5)
                continue

            raw_data = self.db.getset('filtered_candidates', json.dumps([]))
            if not raw_data:
                time.sleep(5)
                continue

            candidates = json.loads(raw_data)

            # --- PORTFOLIO CORRELATION RISK GATE ---
            # Enforce BTC macro bias as a hard code-level rule, not just a prompt suggestion.
            # The AI cannot override this — if conditions aren't met, no GPT calls are made.
            btc_ctx = self._get_btc_context()
            if btc_ctx:
                btc_bias_now = self._get_btc_bias(btc_ctx)
                if btc_bias_now == "STRONG_BEARISH":
                    log.info(
                        "Brain: STRONG_BEARISH BTC — blocking all altcoin analysis "
                        "(correlated drawdown risk too high)"
                    )
                    time.sleep(5)
                    continue
            else:
                btc_bias_now = "NEUTRAL"

            # Do not call OpenAI when at max open orders (no new orders would be placed)
            open_count = shared_db.get_open_order_count()
            max_open = shared_db.get_max_open_orders()

            # Under BEARISH_HEADWIND, halve the effective position limit to reduce
            # simultaneous correlated exposure (all alts will fall together with BTC)
            if btc_bias_now == "BEARISH_HEADWIND":
                max_open = math.ceil(max_open / 2)
                log.info(
                    f"Brain: BEARISH_HEADWIND BTC — reducing effective max_open "
                    f"to {max_open} (correlated risk management)"
                )

            if open_count >= max_open:
                log.info(f"Brain: Skipping AI (max open orders reached: {open_count}/{max_open})")
                self.db.delete('filtered_candidates')
                time.sleep(5)
                continue

            # Market regime gating: block strategies not allowed by current regime,
            # and reduce effective capacity during elevated volatility.
            strategy_now = self._get_strategy_name()
            regime_now = self._get_market_regime()
            if regime_now:
                allowed = regime_now.get('active_strategies', [])
                if allowed and strategy_now not in allowed:
                    log.info(
                        f"Brain: Blocking batch — strategy {strategy_now} not active "
                        f"in {regime_now['regime']} regime (active: {allowed})"
                    )
                    self.db.delete('filtered_candidates')
                    time.sleep(5)
                    continue
                vol = regime_now.get('vol_regime', 'NORMAL')
                if vol == 'EXTREME':
                    max_open = max(1, math.ceil(max_open * 0.5))
                    log.info(f"Brain: EXTREME volatility — reducing effective max_open to {max_open}")
                elif vol == 'ELEVATED':
                    max_open = max(1, math.ceil(max_open * 0.75))
                    log.info(f"Brain: ELEVATED volatility — reducing effective max_open to {max_open}")

            for item in candidates:
                # Re-check at max capacity before each item (avoids race: 10th order opened after batch start)
                open_count = shared_db.get_open_order_count()
                max_open = shared_db.get_max_open_orders()
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

                decision_mode = self._get_decision_mode()
                indicators_dict = {
                    'vwap':          item.get('vwap'),
                    'atr':           item.get('atr'),
                    'ema_stack_15m': item.get('ema_stack_15m'),
                    'ema_stack_1h':  item.get('ema_stack_1h'),
                    'ema_stack_4h':  item.get('ema_stack_4h'),
                    'bollinger_15m': item.get('bollinger_15m'),
                    'macd_15m':      item.get('macd_15m'),
                }
                log.info(f"Brain: Analyzing {symbol} [{decision_mode.upper()}]...")

                if decision_mode == "code":
                    btc_bias_str = btc_bias_now if btc_bias_now else "NEUTRAL"
                    analysis, signal_id = shared_decision.make_decision(
                        symbol=symbol,
                        price=current_price,
                        rsi=item['rsi'],
                        rvol=item['rvol'],
                        candles_15m=item.get('candles_15m', []),
                        candles_1h=item.get('candles_1h', []),
                        high_24h=item.get('high_24h'),
                        low_24h=item.get('low_24h'),
                        indicators=indicators_dict,
                        change_24h=item.get('change_24h'),
                        volume_24h=item.get('volume_24h'),
                        strategy=strategy_now,
                        btc_bias=btc_bias_str,
                        regime_ctx=regime_now,
                    )
                    # Persist code-mode signal to DB for tracking
                    try:
                        with shared_db.get_connection() as conn:
                            shared_db.init_schema(conn)
                            shared_db.insert_signal(
                                conn,
                                signal_id=signal_id,
                                symbol=symbol,
                                stats={
                                    "symbol": symbol, "price": current_price,
                                    "rsi": item['rsi'], "rvol": item['rvol'],
                                    "strategy": strategy_now, "decision_mode": "code",
                                    "bot_version": BOT_VERSION,
                                    "indicators": indicators_dict,
                                },
                                prompt="[code_engine]",
                                response=analysis,
                            )
                    except Exception as e:
                        log.error(f"Brain: Failed to persist code signal {signal_id}: {e}")
                else:
                    analysis, signal_id = self.get_ai_verdict(
                        symbol,
                        current_price,
                        item['rsi'],
                        item['rvol'],
                        item.get('candles_15m', []),
                        item.get('candles_1h', []),
                        item.get('high_24h'),
                        item.get('low_24h'),
                        indicators=indicators_dict,
                        change_24h=item.get('change_24h'),
                        volume_24h=item.get('volume_24h'),
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
                if regime_now:
                    final_signal['position_size_multiplier'] = regime_now.get('position_size_multiplier', 1.0)
                    final_signal['market_regime'] = regime_now.get('regime', 'MIXED')

                # Never send a signal if at max open orders or already have open order for this symbol
                if shared_db.get_open_order_count() >= shared_db.get_max_open_orders():
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
