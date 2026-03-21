"""
Shared configuration used across services (Executor, Monitor, Messenger, Filter, Brain, Scout).
Define numeric defaults, bounds, system setting keys (DB), and Redis key names in one place.
"""

# --- Trading / simulation (Executor, Monitor, Messenger) ---
LEVERAGE = 3
BINANCE_TAKER_FEE = 0.001  # 0.1% taker fee per side (entry and exit)
HOURLY_MARGIN_INTEREST_RATE = 0.00001  # 0.001% per hour simulated margin interest
ENTRY_SLIPPAGE = 0.0005  # 0.05% worse entry for market orders (Executor)
LIQUIDATION_THRESHOLD_PCT = 33.0  # For 3x leverage, skip SL beyond ~33% drop (Executor)
TP_PERCENT = 1.05  # +5% take-profit (Executor default)
SL_PERCENT = 0.98  # -2% stop-loss (Executor default)
POSITION_RISK_PCT = 0.01  # 1% of balance risked per position (Executor position sizing)
RISK_GUARD_MAX_SL = 2.5  # Max allowed stop-loss percent (RiskGuard)
RISK_GUARD_MIN_RR = 1.5  # Min risk/reward ratio (TP/SL) for RiskGuard

# --- Orders / autopilot (Brain, Filter, Messenger) ---
MAX_OPEN_ORDERS_DEFAULT = 4
MAX_OPEN_ORDERS_MIN = 1
MAX_OPEN_ORDERS_MAX = 10

# --- Brain (AI analysis) ---
PRICE_CHANGE_THRESHOLD = 0.005  # 0.5% min price move to re-analyze (skip if change < this)

# --- Symbols (Scout, Messenger) ---
MAX_SYMBOLS_DEFAULT = 25
MAX_SYMBOLS_MIN = 5
MAX_SYMBOLS_MAX = 200

# --- System setting keys (stored in DB settings table) ---
SYSTEM_KEY_MAX_OPEN_ORDERS = "max_open_orders"
SYSTEM_KEY_MAX_SYMBOLS = "max_symbols"
SYSTEM_KEY_TRADING_PAUSED = "trading_paused"
SYSTEM_KEY_AUTOPILOT = "autopilot"
SYSTEM_KEY_BALANCE_LAST_DAY_PNL = "balance_last_day_pnl"
SYSTEM_KEY_BALANCE_LAST_CHECK = "balance_last_check"
SYSTEM_KEY_STRATEGY = "strategy"
SYSTEM_KEY_TIMEZONE_OFFSET_MIN = "timezone_offset_min"
SYSTEM_KEY_SIGNAL_WAIT = "signal_wait"

# --- Redis key (pipeline data; Scout writes, Filter reads) ---
REDIS_KEY_ACTIVE_SYMBOLS = "active_symbols"
REDIS_KEY_BTC_CONTEXT = "btc_context"  # Filter writes, Brain reads for macro bias
