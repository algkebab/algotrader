"""
Shared configuration used across services (Executor, Monitor, Messenger, Filter, Brain, Scout).
Define numeric defaults, bounds, and Redis key names in one place.
"""

# --- Trading / simulation (Executor, Monitor, Messenger) ---
LEVERAGE = 3
BINANCE_SPOT_FEE = 0.001  # 0.1% taker fee per side (entry and exit)
HOURLY_MARGIN_INTEREST_RATE = 0.00001  # 0.001% per hour simulated margin interest
ENTRY_SLIPPAGE = 0.0005  # 0.05% worse entry for market orders (Executor)
LIQUIDATION_THRESHOLD_PCT = 33.0  # For 3x leverage, skip SL beyond ~33% drop (Executor)
TP_PERCENT = 1.05  # +5% take-profit (Executor default)
SL_PERCENT = 0.98  # -2% stop-loss (Executor default)
POSITION_RISK_PCT = 0.01  # 1% of balance risked per position (Executor position sizing)

# --- Orders / autopilot (Brain, Filter, Messenger) ---
MAX_OPEN_ORDERS_DEFAULT = 5
MAX_OPEN_ORDERS_MIN = 1
MAX_OPEN_ORDERS_MAX = 50
ORDER_AMOUNT_DEFAULT = 10
ORDER_AMOUNT_MIN = 1
ORDER_AMOUNT_MAX = 1000

# --- Brain (AI analysis) ---
PRICE_CHANGE_THRESHOLD = 0.005  # 0.5% min price move to re-analyze (skip if change < this)

# --- Symbols (Scout, Messenger) ---
MAX_SYMBOLS_DEFAULT = 25
MAX_SYMBOLS_MIN = 5
MAX_SYMBOLS_MAX = 200

# --- Redis key names (shared across services and clear_redis) ---
REDIS_KEY_MAX_OPEN_ORDERS = "system:max_open_orders"
REDIS_KEY_ORDER_AMOUNT_USDT = "system:order_amount_usdt"
REDIS_KEY_MAX_SYMBOLS = "system:max_symbols"
REDIS_KEY_TRADING_PAUSED = "system:trading_paused"
REDIS_KEY_SUPPRESS_WAIT_SIGNALS = "system:suppress_wait_signals"
REDIS_KEY_AUTOPILOT = "system:autopilot"
REDIS_KEY_MUTED = "system:muted"
REDIS_KEY_BALANCE_LAST_DAY_PNL = "system:balance_last_day_pnl"
REDIS_KEY_BALANCE_LAST_CHECK = "system:balance_last_check"
REDIS_KEY_STRATEGY = "system:strategy"
REDIS_KEY_FILTER_STRATEGY = "system:filter_strategy"
REDIS_KEY_TIMEZONE_OFFSET_MIN = "system:timezone_offset_min"
REDIS_KEY_ACTIVE_SYMBOLS = "system:active_symbols"
