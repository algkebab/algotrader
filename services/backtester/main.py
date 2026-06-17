"""
Walk-forward backtester.

Listens on Redis key 'backtest_request' (blpop).
Fetches historical OHLCV from Binance public API.
Replays through Filter indicators + decision.py code engine.
Simulates limit order fills, TP/SL/time-stop exits.
Stores results in SQLite backtest_runs and backtest_trades tables.
Pushes 'backtest_complete' notification to Redis when done.
"""

import bisect
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import ccxt
import redis
from dotenv import load_dotenv

load_dotenv()

_this_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.abspath(os.path.join(_this_dir, "..", "..")) if os.path.basename(_this_dir) == "backtester" else _this_dir
if _root not in sys.path:
    sys.path.insert(0, _root)

from shared import config as shared_config
from shared import db as shared_db
from shared import decision as shared_decision
from shared import indicators as ind_lib
from shared import logger as shared_logger
from shared import portfolio as portfolio_lib

log = shared_logger.get_logger("backtester")

# Filter strategy profiles (mirrored from filter/main.py)
STRATEGY_PROFILES = {
    "CONSERVATIVE": {
        "min_24h_volume": 10_000_000,
        "rvol_threshold": 1.5,
        "rsi_min": 40,
        "rsi_max": 70,
        "rsi_1h_max": 70,
        "min_change": 0.3,
    },
    "AGGRESSIVE": {
        "min_24h_volume": 5_000_000,
        "rvol_threshold": 1.2,
        "rsi_min": 35,
        "rsi_max": 85,
        "rsi_1h_max": 80,
        "min_change": 2.0,
    },
    "REVERSAL": {
        "min_24h_volume": 20_000_000,
        "rvol_threshold": 3.0,
        "rsi_min": 0,
        "rsi_max": 30,
        "rsi_1h_max": 40,
        "min_change": -2.5,
    },
}

# Default top symbols when none specified in request
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT",
]

# Binance rate limit safety: sleep between symbol fetches
FETCH_SLEEP_S = 0.15

# Step interval for walk-forward: every 4h (in number of 15m bars)
STEP_BARS_15M = 16  # 16 × 15m = 4h

# Maximum simultaneous open positions in AUTO portfolio backtest
MAX_OPEN_PORTFOLIO = 5


def _fetch_ohlcv_full(exchange, symbol: str, timeframe: str, since_ms: int, limit_per_call: int = 1000) -> list:
    """Fetch full OHLCV history from `since_ms` using paginated calls."""
    all_candles = []
    current_since = since_ms
    while True:
        try:
            candles = exchange.fetch_ohlcv(symbol, timeframe, since=current_since, limit=limit_per_call)
        except Exception as e:
            log.warning(f"Backtester: fetch_ohlcv error for {symbol}/{timeframe}: {e}")
            break
        if not candles:
            break
        all_candles.extend(candles)
        if len(candles) < limit_per_call:
            # Got fewer than requested — we've reached the end
            break
        # Advance to just after the last candle's timestamp
        current_since = candles[-1][0] + 1
        time.sleep(FETCH_SLEEP_S)
    # Deduplicate by timestamp (in case of overlap)
    seen = set()
    unique = []
    for c in all_candles:
        if c[0] not in seen:
            seen.add(c[0])
            unique.append(c)
    return sorted(unique, key=lambda x: x[0])


def _passes_filter(candles_15m: list, candles_1h: list, profile: dict, strategy: str) -> bool:
    """Check if symbol passes the Filter criteria at this point in time."""
    if not candles_15m or len(candles_15m) < 20:
        return False

    # Volume check: use average of last 24h as proxy for 24h volume
    avg_vol = sum(c[5] * c[4] for c in candles_15m[-96:]) / max(len(candles_15m[-96:]), 1)
    if avg_vol < profile['min_24h_volume'] / 100:
        # Rough check — backtest uses smaller windows
        pass  # Skip strict volume check in backtest for coverage

    rvol = ind_lib.compute_rvol(candles_15m, period=50)
    rsi = ind_lib.compute_rsi(candles_15m)
    rsi_1h = ind_lib.compute_rsi(candles_1h) if candles_1h else 50.0
    recent_change = ind_lib.compute_recent_change(candles_15m)

    rvol_ok = rvol >= profile['rvol_threshold']
    rsi_1h_ok = rsi_1h <= profile['rsi_1h_max']

    if strategy == "REVERSAL":
        rsi_ok = rsi <= profile['rsi_max']
        change_ok = recent_change <= profile['min_change']
    else:
        rsi_ok = profile['rsi_min'] <= rsi <= profile['rsi_max']
        change_ok = recent_change >= profile['min_change']

    return rvol_ok and rsi_ok and rsi_1h_ok and change_ok


def _backtest_symbol(
    exchange,
    symbol: str,
    strategy: str,
    days: int,
    initial_balance: float,
) -> tuple[list, float]:
    """
    Walk-forward backtest for one symbol.
    Returns (trades: list, final_balance: float).
    """
    profile = STRATEGY_PROFILES.get(strategy, STRATEGY_PROFILES["CONSERVATIVE"])

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    # Fetch with warmup: extra days so EMAs are seeded properly
    since_15m = now_ms - (days + 2) * 24 * 3600 * 1000
    since_1h = now_ms - (days + 7) * 24 * 3600 * 1000
    since_4h = now_ms - (days + 30) * 24 * 3600 * 1000

    log.info(f"Backtester: Fetching {symbol} history ({days}d)...")
    candles_15m_all = _fetch_ohlcv_full(exchange, symbol, "15m", since_15m)
    time.sleep(FETCH_SLEEP_S)
    candles_1h_all = _fetch_ohlcv_full(exchange, symbol, "1h", since_1h)
    time.sleep(FETCH_SLEEP_S)
    candles_4h_all = _fetch_ohlcv_full(exchange, symbol, "4h", since_4h)
    time.sleep(FETCH_SLEEP_S)

    if not candles_15m_all or len(candles_15m_all) < 100:
        log.warning(f"Backtester: Insufficient 15m data for {symbol} ({len(candles_15m_all)} bars)")
        return [], initial_balance

    # Trim 15m to only the backtest period (drop warmup)
    start_cutoff_ms = now_ms - days * 24 * 3600 * 1000
    backtest_start_idx = next(
        (i for i, c in enumerate(candles_15m_all) if c[0] >= start_cutoff_ms),
        len(candles_15m_all) - STEP_BARS_15M * 2,
    )

    trades = []
    balance = initial_balance
    open_position = None  # dict with entry details

    # Walk forward in 4h steps from start_cutoff
    for step_idx in range(backtest_start_idx, len(candles_15m_all) - STEP_BARS_15M, STEP_BARS_15M):
        current_ts_ms = candles_15m_all[step_idx][0]

        # Rolling windows for indicator computation
        c15 = candles_15m_all[max(0, step_idx - 150):step_idx]
        c1h = [c for c in candles_1h_all if c[0] <= current_ts_ms][-150:]
        c4h = [c for c in candles_4h_all if c[0] <= current_ts_ms][-100:]

        if not c15 or len(c15) < 60:
            continue

        # Check open position exits against bars in this 4h window
        if open_position is not None:
            # Examine each 15m bar in this step for TP/SL/time-stop
            window_bars = candles_15m_all[step_idx:step_idx + STEP_BARS_15M]
            for bar in window_bars:
                bar_low = bar[3]
                bar_high = bar[2]
                bar_close = bar[4]
                bar_ts = bar[0]

                sl_hit = bar_low <= open_position['sl_price']
                tp_hit = bar_high >= open_position['tp_price']
                time_stop = (bar_ts - open_position['entry_ts_ms']) >= 48 * 3600 * 1000

                if sl_hit or tp_hit or time_stop:
                    if sl_hit and tp_hit:
                        # Both in same bar — assume worst case (SL hit)
                        exit_price = open_position['sl_price']
                        close_reason = "STOP-LOSS"
                    elif sl_hit:
                        exit_price = open_position['sl_price']
                        close_reason = "STOP-LOSS"
                    elif tp_hit:
                        exit_price = open_position['tp_price']
                        close_reason = "TAKE-PROFIT"
                    else:
                        exit_price = bar_close
                        close_reason = "TIME-STOP"

                    qty = open_position['quantity']
                    notional_usdt = open_position['notional_usdt']
                    entry_price = open_position['entry_price']

                    gross_pnl = (exit_price - entry_price) * qty
                    exit_notional = qty * exit_price
                    exit_fee = exit_notional * shared_config.MAKER_FEE
                    pnl_usdt = gross_pnl - exit_fee
                    pnl_pct = (exit_price - entry_price) / entry_price * 100

                    margin_usdt = notional_usdt / shared_config.LEVERAGE
                    balance += margin_usdt + pnl_usdt

                    exit_dt = datetime.fromtimestamp(bar_ts / 1000, tz=timezone.utc)
                    trades.append({
                        "symbol": symbol,
                        "strategy": strategy,
                        "entry_time": open_position['entry_time'],
                        "exit_time": exit_dt.isoformat(),
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "sl_price": open_position['sl_price'],
                        "tp_price": open_position['tp_price'],
                        "quantity": qty,
                        "notional_usdt": notional_usdt,
                        "pnl_usdt": round(pnl_usdt, 4),
                        "pnl_pct": round(pnl_pct, 4),
                        "close_reason": close_reason,
                        "confidence": open_position.get('confidence'),
                        "setup_grade": open_position.get('setup_grade'),
                        "sl_pct": open_position.get('sl_pct'),
                        "tp_pct": open_position.get('tp_pct'),
                    })
                    open_position = None
                    break

        # Only open new position if none is open
        if open_position is not None:
            continue

        # Check filter criteria
        if not _passes_filter(c15, c1h, profile, strategy):
            continue

        # Compute indicators and run decision engine
        indicators = ind_lib.compute_all_indicators(c15, c1h, c4h, as_of_ts_ms=current_ts_ms)
        rsi_val = ind_lib.compute_rsi(c15)
        rvol_val = ind_lib.compute_rvol(c15)

        current_price = c15[-1][4]

        try:
            analysis, _ = shared_decision.make_decision(
                symbol=symbol,
                price=current_price,
                rsi=rsi_val,
                rvol=rvol_val,
                candles_15m=c15,
                candles_1h=c1h,
                high_24h=max(c[2] for c in c15[-96:]) if c15 else None,
                low_24h=min(c[3] for c in c15[-96:]) if c15 else None,
                indicators=indicators,
                strategy=strategy,
                btc_bias="NEUTRAL",  # simplified for backtest
            )
        except Exception as e:
            log.warning(f"Backtester: Decision engine error for {symbol}: {e}")
            continue

        if analysis.get('verdict') != 'BUY':
            continue

        sl_pct = float(analysis.get('stop_loss_pct') or 1.5)
        tp_pct = float(analysis.get('take_profit_pct') or 3.0)

        # Entry at next bar's open (cleaner than intrabar)
        if step_idx + STEP_BARS_15M >= len(candles_15m_all):
            continue
        entry_bar = candles_15m_all[step_idx + 1]
        entry_price = entry_bar[1]  # open of next bar
        entry_ts_ms = entry_bar[0]

        sl_price = entry_price * (1 - sl_pct / 100)
        tp_price = entry_price * (1 + tp_pct / 100)

        # Position sizing: fixed POSITION_RISK_PCT for backtest
        risk_amount = balance * shared_config.POSITION_RISK_PCT
        if sl_pct <= 0:
            continue
        notional_usdt = min(risk_amount / (sl_pct / 100), balance * shared_config.LEVERAGE)
        if notional_usdt <= 0:
            continue

        margin_usdt = notional_usdt / shared_config.LEVERAGE
        qty = notional_usdt / entry_price
        entry_fee = notional_usdt * shared_config.MAKER_FEE
        total_cost = margin_usdt + entry_fee

        if balance < total_cost:
            continue

        balance -= total_cost
        entry_dt = datetime.fromtimestamp(entry_ts_ms / 1000, tz=timezone.utc)

        open_position = {
            'entry_price': entry_price,
            'entry_ts_ms': entry_ts_ms,
            'entry_time': entry_dt.isoformat(),
            'sl_price': sl_price,
            'tp_price': tp_price,
            'quantity': qty,
            'notional_usdt': notional_usdt,
            'confidence': analysis.get('confidence'),
            'setup_grade': analysis.get('setup_grade'),
            'sl_pct': sl_pct,
            'tp_pct': tp_pct,
        }

    # Close any still-open position at last bar's close (end of backtest)
    if open_position is not None and candles_15m_all:
        last_bar = candles_15m_all[-1]
        exit_price = last_bar[4]
        qty = open_position['quantity']
        notional_usdt = open_position['notional_usdt']
        entry_price = open_position['entry_price']
        gross_pnl = (exit_price - entry_price) * qty
        exit_fee = qty * exit_price * shared_config.MAKER_FEE
        pnl_usdt = gross_pnl - exit_fee
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        margin_usdt = notional_usdt / shared_config.LEVERAGE
        balance += margin_usdt + pnl_usdt
        exit_dt = datetime.fromtimestamp(last_bar[0] / 1000, tz=timezone.utc)
        trades.append({
            "symbol": symbol,
            "strategy": strategy,
            "entry_time": open_position['entry_time'],
            "exit_time": exit_dt.isoformat(),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "sl_price": open_position['sl_price'],
            "tp_price": open_position['tp_price'],
            "quantity": qty,
            "notional_usdt": notional_usdt,
            "pnl_usdt": round(pnl_usdt, 4),
            "pnl_pct": round(pnl_pct, 4),
            "close_reason": "END-OF-BACKTEST",
            "confidence": open_position.get('confidence'),
            "setup_grade": open_position.get('setup_grade'),
            "sl_pct": open_position.get('sl_pct'),
            "tp_pct": open_position.get('tp_pct'),
        })

    return trades, balance


def _compute_regime_from_slices(c4h_btc: list, c15_btc: list, sym_slices: dict) -> dict:
    """Compute market regime from pre-sliced BTC candles and symbol breadth slices.

    Mirrors filter._compute_and_store_market_regime() but operates on historical data.
    sym_slices: {symbol: (c15_slice, c4h_slice)} already trimmed to current timestamp.
    """
    if len(c4h_btc) < 30:
        return {
            "regime": "MIXED", "confidence": 40,
            "active_strategies": ["CONSERVATIVE", "REVERSAL"],
            "position_size_multiplier": 0.75, "vol_regime": "NORMAL",
            "btc_4h_align": "MIXED", "adx_4h": 0.0, "breadth_bull_pct": 50.0,
        }

    closes_4h    = [c[4] for c in c4h_btc]
    ema_stack_4h = ind_lib.compute_ema_stack(closes_4h)
    adx_4h       = ind_lib.compute_adx(c4h_btc) or 0.0
    btc_rsi      = ind_lib.compute_rsi(c15_btc) if len(c15_btc) >= 15 else 50.0

    atr_ratio = 1.0
    if len(c4h_btc) >= 65:
        recent_ret = [(c4h_btc[i][4] - c4h_btc[i-1][4]) / c4h_btc[i-1][4]
                      for i in range(len(c4h_btc) - 14, len(c4h_btc))]
        hist_ret   = [(c4h_btc[i][4] - c4h_btc[i-1][4]) / c4h_btc[i-1][4]
                      for i in range(len(c4h_btc) - 50, len(c4h_btc))]
        vol_r = (sum(r * r for r in recent_ret) / len(recent_ret)) ** 0.5
        vol_h = (sum(r * r for r in hist_ret)   / len(hist_ret))   ** 0.5
        atr_ratio = vol_r / vol_h if vol_h > 0 else 1.0

    bullish_4h_count = rsi_above_50_count = total = 0
    for sym, (c15_sym, c4h_sym) in sym_slices.items():
        if len(c4h_sym) < 50:
            continue
        total += 1
        ema = ind_lib.compute_ema_stack([c[4] for c in c4h_sym])
        if ema and ema.get('alignment') in ('BULLISH', 'RECOVERING'):
            bullish_4h_count += 1
        if len(c15_sym) >= 15 and ind_lib.compute_rsi(c15_sym) >= 50:
            rsi_above_50_count += 1

    breadth_bull_pct = round(bullish_4h_count / total * 100, 1) if total > 0 else 50.0
    breadth_rsi_pct  = round(rsi_above_50_count / total * 100, 1) if total > 0 else 50.0
    btc_4h_align     = (ema_stack_4h or {}).get('alignment', 'MIXED')

    vol_regime = "EXTREME" if atr_ratio > 2.0 else "ELEVATED" if atr_ratio > 1.5 else "NORMAL"

    bull_votes = bear_votes = 0
    if btc_4h_align in ('BULLISH', 'RECOVERING'):                           bull_votes += 1
    if btc_4h_align in ('BEARISH', 'WEAKENING'):                            bear_votes += 1
    if breadth_bull_pct >= 55:                                               bull_votes += 1
    if breadth_bull_pct <= 40:                                               bear_votes += 1
    if adx_4h >= 25 and btc_4h_align in ('BULLISH', 'RECOVERING'):        bull_votes += 1
    if adx_4h >= 25 and btc_4h_align in ('BEARISH', 'WEAKENING'):         bear_votes += 1
    if btc_rsi >= 50:                                                        bull_votes += 1
    if btc_rsi < 50:                                                         bear_votes += 1
    if breadth_rsi_pct >= 55:                                                bull_votes += 1
    if breadth_rsi_pct < 45:                                                 bear_votes += 1

    if 0 < adx_4h < 20:
        regime     = "RANGING"
        confidence = max(30, int((1 - adx_4h / 20) * 80))
    elif bull_votes >= 3:
        regime     = "BULL_TRENDING"
        confidence = int(bull_votes / 5 * 100)
    elif bear_votes >= 3:
        regime     = "BEAR_TRENDING"
        confidence = int(bear_votes / 5 * 100)
    else:
        regime     = "MIXED"
        confidence = 40

    if regime == "BULL_TRENDING":
        active_strategies, size_mult = ["CONSERVATIVE", "REVERSAL"], 1.0
    elif regime == "BEAR_TRENDING":
        active_strategies, size_mult = [], 0.5  # No longs in bear — all wins were TS exits, not TP
    elif regime == "RANGING":
        active_strategies, size_mult = ["REVERSAL"], 0.75
    else:
        active_strategies, size_mult = ["CONSERVATIVE"], 0.75

    if vol_regime == "EXTREME":
        size_mult = round(size_mult * 0.5, 2)
    elif vol_regime == "ELEVATED":
        size_mult = round(size_mult * 0.75, 2)

    return {
        "regime": regime, "confidence": confidence,
        "active_strategies": active_strategies,
        "position_size_multiplier": size_mult,
        "vol_regime": vol_regime,
        "btc_4h_align": btc_4h_align,
        "adx_4h": round(adx_4h, 2),
        "breadth_bull_pct": breadth_bull_pct,
    }


def _backtest_portfolio(
    exchange,
    symbols: list,
    days: int,
    initial_balance: float,
) -> tuple[list, float]:
    """Portfolio-level walk-forward backtest with regime-switching strategy selection.

    Uses a single shared balance and position cap (MAX_OPEN_PORTFOLIO).
    At each 4h step: computes market regime from BTC historical data + symbol breadth,
    selects allowed strategies, evaluates all symbols, opens trades from active strategies.
    Returns (all_trades, final_balance).
    """
    now_ms          = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_cutoff_ms = now_ms - days * 24 * 3600 * 1000

    log.info("Backtester[AUTO]: Fetching BTC candles for regime detection...")
    btc_4h  = _fetch_ohlcv_full(exchange, "BTC/USDT", "4h",
                                  now_ms - (days + 30) * 24 * 3600 * 1000)
    time.sleep(FETCH_SLEEP_S)
    btc_15m = _fetch_ohlcv_full(exchange, "BTC/USDT", "15m",
                                  now_ms - (days + 2) * 24 * 3600 * 1000)
    time.sleep(FETCH_SLEEP_S)

    all_symbol_data = {}
    for sym in symbols:
        log.info(f"Backtester[AUTO]: Fetching {sym}...")
        c15 = _fetch_ohlcv_full(exchange, sym, "15m",
                                  now_ms - (days + 2) * 24 * 3600 * 1000)
        time.sleep(FETCH_SLEEP_S)
        c1h = _fetch_ohlcv_full(exchange, sym, "1h",
                                  now_ms - (days + 7) * 24 * 3600 * 1000)
        time.sleep(FETCH_SLEEP_S)
        c4h = _fetch_ohlcv_full(exchange, sym, "4h",
                                  now_ms - (days + 30) * 24 * 3600 * 1000)
        time.sleep(FETCH_SLEEP_S)
        if c15 and len(c15) >= 100:
            all_symbol_data[sym] = (c15, c1h, c4h)
        else:
            log.warning(f"Backtester[AUTO]: Insufficient data for {sym}, skipping")

    if not all_symbol_data or not btc_15m:
        log.warning("Backtester[AUTO]: No usable data, aborting")
        return [], initial_balance

    # Precompute timestamp arrays once for O(log n) bisect lookups in the main loop
    btc_ts_4h  = [c[0] for c in btc_4h]
    btc_ts_15m = [c[0] for c in btc_15m]
    sym_ts = {
        sym: (
            [c[0] for c in d[0]],
            [c[0] for c in d[1]],
            [c[0] for c in d[2]],
        )
        for sym, d in all_symbol_data.items()
    }

    backtest_start_idx = bisect.bisect_left(btc_ts_15m, start_cutoff_ms)
    backtest_start_idx = max(backtest_start_idx, 150)

    all_trades      = []
    balance         = initial_balance
    open_positions  = {}  # symbol -> position dict
    prev_regime_name = None

    for step_idx in range(backtest_start_idx, len(btc_15m) - STEP_BARS_15M, STEP_BARS_15M):
        current_ts_ms = btc_15m[step_idx][0]
        next_ts_ms    = btc_15m[min(step_idx + STEP_BARS_15M, len(btc_15m) - 1)][0]

        # 1. Check exits for all open positions
        for sym in list(open_positions.keys()):
            pos    = open_positions[sym]
            c15s   = all_symbol_data[sym][0]
            ts15s  = sym_ts[sym][0]
            start_i = bisect.bisect_right(ts15s, pos['entry_ts_ms'])
            end_i   = bisect.bisect_right(ts15s, next_ts_ms)

            for bar in c15s[start_i:end_i]:
                bar_low, bar_high, bar_close, bar_ts = bar[3], bar[2], bar[4], bar[0]
                sl_hit    = bar_low  <= pos['sl_price']
                tp_hit    = bar_high >= pos['tp_price']
                time_stop = (bar_ts - pos['entry_ts_ms']) >= 48 * 3600 * 1000

                if not (sl_hit or tp_hit or time_stop):
                    continue

                if sl_hit and tp_hit:
                    exit_price, close_reason = pos['sl_price'], "STOP-LOSS"
                elif sl_hit:
                    exit_price, close_reason = pos['sl_price'], "STOP-LOSS"
                elif tp_hit:
                    exit_price, close_reason = pos['tp_price'], "TAKE-PROFIT"
                else:
                    exit_price, close_reason = bar_close, "TIME-STOP"

                qty         = pos['quantity']
                notional    = pos['notional_usdt']
                entry_price = pos['entry_price']
                gross_pnl   = (exit_price - entry_price) * qty
                exit_fee    = qty * exit_price * shared_config.MAKER_FEE
                pnl_usdt    = gross_pnl - exit_fee
                pnl_pct     = (exit_price - entry_price) / entry_price * 100
                balance    += notional / shared_config.LEVERAGE + pnl_usdt

                exit_dt = datetime.fromtimestamp(bar_ts / 1000, tz=timezone.utc)
                all_trades.append({
                    "symbol":        sym,
                    "strategy":      pos['strategy'],
                    "regime":        pos.get('regime', 'MIXED'),
                    "entry_time":    pos['entry_time'],
                    "exit_time":     exit_dt.isoformat(),
                    "entry_price":   entry_price,
                    "exit_price":    exit_price,
                    "sl_price":      pos['sl_price'],
                    "tp_price":      pos['tp_price'],
                    "quantity":      qty,
                    "notional_usdt": notional,
                    "pnl_usdt":      round(pnl_usdt, 4),
                    "pnl_pct":       round(pnl_pct, 4),
                    "close_reason":  close_reason,
                    "confidence":    pos.get('confidence'),
                    "setup_grade":   pos.get('setup_grade'),
                    "sl_pct":        pos.get('sl_pct'),
                    "tp_pct":        pos.get('tp_pct'),
                })
                del open_positions[sym]
                break

        # 2. Compute market regime at this timestamp
        c4h_end   = bisect.bisect_right(btc_ts_4h, current_ts_ms)
        c15_end   = bisect.bisect_right(btc_ts_15m, current_ts_ms)
        c4h_slice = btc_4h[max(0, c4h_end - 100):c4h_end]
        c15_slice = btc_15m[max(0, c15_end - 150):c15_end]

        sym_slices = {}
        for sym, (c15s, _, c4hs) in all_symbol_data.items():
            i15 = bisect.bisect_right(sym_ts[sym][0], current_ts_ms)
            i4  = bisect.bisect_right(sym_ts[sym][2], current_ts_ms)
            sym_slices[sym] = (
                c15s[max(0, i15 - 60):i15],
                c4hs[max(0, i4  - 60):i4],
            )

        regime = _compute_regime_from_slices(c4h_slice, c15_slice, sym_slices)
        if regime['regime'] != prev_regime_name:
            ts_str = datetime.fromtimestamp(current_ts_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
            log.info(
                f"Backtester[AUTO]: Regime → {regime['regime']} @ {ts_str} "
                f"| ADX={regime['adx_4h']:.1f} | BTC_4h={regime['btc_4h_align']} "
                f"| Breadth={regime['breadth_bull_pct']:.0f}%"
            )
            prev_regime_name = regime['regime']

        if len(open_positions) >= MAX_OPEN_PORTFOLIO:
            continue

        allowed_strategies = regime['active_strategies']
        size_mult = regime['position_size_multiplier']
        btc_align = regime.get('btc_4h_align', 'MIXED')
        btc_bias  = (
            "BULLISH_TAILWIND" if btc_align in ('BULLISH', 'RECOVERING') else
            "BEARISH_HEADWIND" if btc_align in ('BEARISH', 'WEAKENING') else
            "NEUTRAL"
        )

        # 3. Scan symbols for new entries
        for sym, (c15s, c1hs, c4hs) in all_symbol_data.items():
            if sym in open_positions or len(open_positions) >= MAX_OPEN_PORTFOLIO:
                break

            i15 = bisect.bisect_right(sym_ts[sym][0], current_ts_ms)
            i1h = bisect.bisect_right(sym_ts[sym][1], current_ts_ms)
            i4  = bisect.bisect_right(sym_ts[sym][2], current_ts_ms)
            c15 = c15s[max(0, i15 - 150):i15]
            c1h = c1hs[max(0, i1h - 150):i1h]
            c4h = c4hs[max(0, i4  - 100):i4]

            if len(c15) < 60:
                continue

            for strategy in allowed_strategies:
                profile = STRATEGY_PROFILES[strategy]
                if not _passes_filter(c15, c1h, profile, strategy):
                    continue

                indicators = ind_lib.compute_all_indicators(
                    c15, c1h, c4h, as_of_ts_ms=current_ts_ms
                )
                rsi_val   = ind_lib.compute_rsi(c15)
                rvol_val  = ind_lib.compute_rvol(c15)
                cur_price = c15[-1][4]
                high_24h  = max(c[2] for c in c15[-96:]) if len(c15) >= 96 else max(c[2] for c in c15)
                low_24h   = min(c[3] for c in c15[-96:]) if len(c15) >= 96 else min(c[3] for c in c15)

                try:
                    analysis, _ = shared_decision.make_decision(
                        symbol=sym, price=cur_price, rsi=rsi_val, rvol=rvol_val,
                        candles_15m=c15, candles_1h=c1h,
                        high_24h=high_24h, low_24h=low_24h,
                        indicators=indicators, strategy=strategy,
                        btc_bias=btc_bias, regime_ctx=regime,
                    )
                except Exception as e:
                    log.warning(f"Backtester[AUTO]: Decision error {sym}/{strategy}: {e}")
                    continue

                if analysis.get('verdict') != 'BUY':
                    continue

                sl_pct = float(analysis.get('stop_loss_pct') or 1.5)
                tp_pct = float(analysis.get('take_profit_pct') or 3.0)
                if sl_pct <= 0:
                    continue

                next_bar_i = bisect.bisect_right(sym_ts[sym][0], current_ts_ms)
                if next_bar_i >= len(c15s):
                    continue
                entry_bar   = c15s[next_bar_i]
                entry_price = entry_bar[1]
                entry_ts_ms = entry_bar[0]

                sl_price = entry_price * (1 - sl_pct / 100)
                tp_price = entry_price * (1 + tp_pct / 100)

                risk_amount   = balance * shared_config.POSITION_RISK_PCT * size_mult
                notional_usdt = min(risk_amount / (sl_pct / 100),
                                    balance * shared_config.LEVERAGE)
                if notional_usdt <= 0:
                    continue
                margin_usdt = notional_usdt / shared_config.LEVERAGE
                qty         = notional_usdt / entry_price
                entry_fee   = notional_usdt * shared_config.MAKER_FEE
                total_cost  = margin_usdt + entry_fee
                if balance < total_cost:
                    continue

                balance -= total_cost
                entry_dt = datetime.fromtimestamp(entry_ts_ms / 1000, tz=timezone.utc)
                open_positions[sym] = {
                    'strategy':      strategy,
                    'regime':        regime['regime'],
                    'entry_price':   entry_price,
                    'entry_ts_ms':   entry_ts_ms,
                    'entry_time':    entry_dt.isoformat(),
                    'sl_price':      sl_price,
                    'tp_price':      tp_price,
                    'quantity':      qty,
                    'notional_usdt': notional_usdt,
                    'confidence':    analysis.get('confidence'),
                    'setup_grade':   analysis.get('setup_grade'),
                    'sl_pct':        sl_pct,
                    'tp_pct':        tp_pct,
                }
                break  # one strategy per symbol per step

    # Close any still-open positions at end of backtest
    for sym, pos in open_positions.items():
        c15s = all_symbol_data[sym][0]
        if not c15s:
            continue
        last_bar    = c15s[-1]
        exit_price  = last_bar[4]
        qty         = pos['quantity']
        notional    = pos['notional_usdt']
        entry_price = pos['entry_price']
        gross_pnl   = (exit_price - entry_price) * qty
        exit_fee    = qty * exit_price * shared_config.MAKER_FEE
        pnl_usdt    = gross_pnl - exit_fee
        pnl_pct     = (exit_price - entry_price) / entry_price * 100
        balance    += notional / shared_config.LEVERAGE + pnl_usdt
        exit_dt     = datetime.fromtimestamp(last_bar[0] / 1000, tz=timezone.utc)
        all_trades.append({
            "symbol":        sym,
            "strategy":      pos['strategy'],
            "regime":        pos.get('regime', 'MIXED'),
            "entry_time":    pos['entry_time'],
            "exit_time":     exit_dt.isoformat(),
            "entry_price":   entry_price,
            "exit_price":    exit_price,
            "sl_price":      pos['sl_price'],
            "tp_price":      pos['tp_price'],
            "quantity":      qty,
            "notional_usdt": notional,
            "pnl_usdt":      round(pnl_usdt, 4),
            "pnl_pct":       round(pnl_pct, 4),
            "close_reason":  "END-OF-BACKTEST",
            "confidence":    pos.get('confidence'),
            "setup_grade":   pos.get('setup_grade'),
            "sl_pct":        pos.get('sl_pct'),
            "tp_pct":        pos.get('tp_pct'),
        })

    return all_trades, balance


def _store_results(
    strategy: str,
    symbols: list,
    days: int,
    initial_balance: float,
    final_balance: float,
    all_trades: list,
    benchmark_return_pct: float,
) -> int:
    """Store backtest run + trades in DB. Returns run_id."""
    total_trades = len(all_trades)
    wins = [t for t in all_trades if t.get('pnl_usdt', 0) > 0]
    win_rate = len(wins) / total_trades if total_trades > 0 else None
    total_return_pct = (final_balance - initial_balance) / initial_balance * 100 if initial_balance > 0 else 0.0
    alpha = portfolio_lib.compute_alpha(total_return_pct, benchmark_return_pct)

    daily_returns = _daily_returns_from_trades(all_trades, initial_balance, days)
    sharpe = portfolio_lib.compute_sharpe(daily_returns) if daily_returns else None

    # Equity curve for max drawdown
    equity = [initial_balance]
    running = initial_balance
    for t in sorted(all_trades, key=lambda x: x.get('entry_time', '')):
        running += t.get('pnl_usdt', 0)
        equity.append(running)
    max_dd = portfolio_lib.compute_peak_drawdown(equity)

    with shared_db.get_connection() as conn:
        shared_db.init_schema(conn)
        now_iso = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """INSERT INTO backtest_runs
               (strategy, symbol, days, initial_balance, final_balance, total_trades,
                win_rate, sharpe, max_drawdown_pct, total_return_pct,
                benchmark_return_pct, alpha, params_json, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                strategy,
                ",".join(symbols) if symbols else None,
                days,
                initial_balance,
                round(final_balance, 2),
                total_trades,
                round(win_rate, 3) if win_rate is not None else None,
                sharpe,
                max_dd,
                round(total_return_pct, 2),
                round(benchmark_return_pct, 2),
                round(alpha, 2),
                json.dumps({"symbols": symbols}),
                now_iso,
            ),
        )
        run_id = cur.lastrowid

        for t in all_trades:
            conn.execute(
                """INSERT INTO backtest_trades
                   (run_id, symbol, strategy, regime, entry_time, exit_time, entry_price, exit_price,
                    sl_price, tp_price, quantity, notional_usdt, pnl_usdt, pnl_pct,
                    close_reason, confidence, setup_grade, sl_pct, tp_pct)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, t['symbol'], t['strategy'], t.get('regime'),
                    t['entry_time'], t.get('exit_time'),
                    t['entry_price'], t.get('exit_price'), t['sl_price'], t['tp_price'],
                    t['quantity'], t['notional_usdt'], t.get('pnl_usdt'), t.get('pnl_pct'),
                    t.get('close_reason'), t.get('confidence'), t.get('setup_grade'),
                    t.get('sl_pct'), t.get('tp_pct'),
                ),
            )

    return run_id


def _daily_returns_from_trades(all_trades: list, initial_balance: float, days: int) -> list:
    """Build a daily PnL% series covering `days` calendar days.

    Trades are bucketed by exit date. Days with no closed trades contribute 0%.
    This gives compute_sharpe the full time-series so idle days suppress the ratio.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    # Map each trade's PnL to its exit date
    daily_pnl: dict[str, float] = {}
    for t in all_trades:
        exit_time = t.get('exit_time')
        if not exit_time:
            continue
        try:
            dt = datetime.fromisoformat(exit_time).date()
        except ValueError:
            continue
        key = dt.isoformat()
        daily_pnl[key] = daily_pnl.get(key, 0.0) + float(t.get('pnl_usdt', 0.0))

    # Build day-by-day equity and compute daily return %
    returns = []
    equity = initial_balance
    for offset in range(days):
        day = (start + timedelta(days=offset + 1)).date().isoformat()
        pnl = daily_pnl.get(day, 0.0)
        day_return_pct = (pnl / equity * 100) if equity > 0 else 0.0
        equity = max(equity + pnl, 0.01)
        returns.append(day_return_pct)
    return returns


def _get_benchmark_return(exchange, days: int) -> float:
    """Fetch BTC price at start and end of period. Returns return %."""
    try:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        since_ms = now_ms - (days + 1) * 24 * 3600 * 1000
        btc_candles = _fetch_ohlcv_full(exchange, "BTC/USDT", "1d", since_ms)
        if btc_candles and len(btc_candles) >= 2:
            start_price = btc_candles[0][4]  # close of first day
            end_price = btc_candles[-1][4]   # close of last day
            if start_price > 0:
                return round((end_price - start_price) / start_price * 100, 2)
    except Exception as e:
        log.warning(f"Backtester: Could not fetch BTC benchmark: {e}")
    return 0.0


class Backtester:
    def __init__(self):
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        # Public Binance API — no keys needed
        self.exchange = ccxt.binance({'enableRateLimit': True})

    def process_request(self, request: dict) -> None:
        strategy = request.get('strategy', 'CONSERVATIVE').upper()
        symbols = request.get('symbols') or DEFAULT_SYMBOLS
        days = int(request.get('days', 90))
        initial_balance = float(request.get('initial_balance', 1000.0))

        if strategy != "AUTO" and strategy not in STRATEGY_PROFILES:
            log.warning(f"Backtester: Unknown strategy {strategy}, using CONSERVATIVE")
            strategy = 'CONSERVATIVE'

        days = max(7, min(days, 365))
        log.info(f"Backtester: Starting {strategy} backtest | {len(symbols)} symbols | {days}d | ${initial_balance:.0f}")

        # Fetch benchmark once
        benchmark_return = _get_benchmark_return(self.exchange, days)
        log.info(f"Backtester: BTC benchmark return over {days}d: {benchmark_return:+.1f}%")

        if strategy == "AUTO":
            all_trades, final_balance = _backtest_portfolio(
                self.exchange, symbols, days, initial_balance
            )

            breakdown = {}
            for t in all_trades:
                s = t.get('strategy', 'UNKNOWN')
                if s not in breakdown:
                    breakdown[s] = {'trades': 0, 'wins': 0, 'pnl_usdt': 0.0}
                breakdown[s]['trades'] += 1
                if t.get('pnl_usdt', 0) > 0:
                    breakdown[s]['wins'] += 1
                breakdown[s]['pnl_usdt'] += t.get('pnl_usdt', 0)

            run_id = _store_results(
                strategy="AUTO", symbols=symbols, days=days,
                initial_balance=initial_balance, final_balance=final_balance,
                all_trades=all_trades, benchmark_return_pct=benchmark_return,
            )

            total_trades = len(all_trades)
            wins         = [t for t in all_trades if t.get('pnl_usdt', 0) > 0]
            win_rate     = len(wins) / total_trades if total_trades > 0 else 0.0
            total_return_pct = (final_balance - initial_balance) / initial_balance * 100 if initial_balance > 0 else 0.0
            alpha        = portfolio_lib.compute_alpha(total_return_pct, benchmark_return)
            daily_returns = _daily_returns_from_trades(all_trades, initial_balance, days)
            sharpe       = portfolio_lib.compute_sharpe(daily_returns) if daily_returns else None
            equity       = [initial_balance]
            running      = initial_balance
            for t in sorted(all_trades, key=lambda x: x.get('entry_time', '')):
                running += t.get('pnl_usdt', 0)
                equity.append(running)
            max_dd = portfolio_lib.compute_peak_drawdown(equity)

            self.db.rpush("notifications", json.dumps({
                "type": "backtest_complete",
                "data": {
                    "run_id": run_id, "strategy": "AUTO", "days": days,
                    "total_trades": total_trades,
                    "win_rate": round(win_rate, 3),
                    "total_return_pct": round(total_return_pct, 2),
                    "benchmark_return_pct": round(benchmark_return, 2),
                    "alpha": round(alpha, 2),
                    "sharpe": sharpe,
                    "max_drawdown_pct": max_dd,
                    "strategy_breakdown": {
                        s: {
                            "trades":   v['trades'],
                            "win_rate": round(v['wins'] / v['trades'], 3) if v['trades'] > 0 else 0.0,
                            "pnl_usdt": round(v['pnl_usdt'], 2),
                        }
                        for s, v in breakdown.items()
                    },
                },
            }))
            log.info(
                f"Backtester: AUTO run {run_id} complete — "
                f"{total_trades} trades | WR={win_rate*100:.0f}% | "
                f"Return={total_return_pct:+.1f}% | Alpha={alpha:+.1f}% | Sharpe={sharpe}"
            )
            return

        all_trades = []
        combined_balance = initial_balance
        balance_per_symbol = initial_balance / max(len(symbols), 1)

        for i, symbol in enumerate(symbols, 1):
            try:
                trades, final_bal = _backtest_symbol(
                    self.exchange, symbol, strategy, days, balance_per_symbol
                )
                all_trades.extend(trades)
                # Aggregate: ratio of final to initial per symbol
                if balance_per_symbol > 0:
                    combined_balance += (final_bal - balance_per_symbol)
                if i % 10 == 0:
                    log.info(f"Backtester: Progress {i}/{len(symbols)} symbols processed")
            except Exception as e:
                log.warning(f"Backtester: Error processing {symbol}: {e}")
            time.sleep(FETCH_SLEEP_S)

        run_id = _store_results(
            strategy=strategy,
            symbols=symbols,
            days=days,
            initial_balance=initial_balance,
            final_balance=combined_balance,
            all_trades=all_trades,
            benchmark_return_pct=benchmark_return,
        )

        total_trades = len(all_trades)
        wins = [t for t in all_trades if t.get('pnl_usdt', 0) > 0]
        win_rate = len(wins) / total_trades if total_trades > 0 else 0.0
        total_return_pct = (combined_balance - initial_balance) / initial_balance * 100 if initial_balance > 0 else 0.0
        alpha = portfolio_lib.compute_alpha(total_return_pct, benchmark_return)

        daily_returns = _daily_returns_from_trades(all_trades, initial_balance, days)
        sharpe = portfolio_lib.compute_sharpe(daily_returns) if daily_returns else None

        equity = [initial_balance]
        running = initial_balance
        for t in sorted(all_trades, key=lambda x: x.get('entry_time', '')):
            running += t.get('pnl_usdt', 0)
            equity.append(running)
        max_dd = portfolio_lib.compute_peak_drawdown(equity)

        notification = {
            "type": "backtest_complete",
            "data": {
                "run_id": run_id,
                "strategy": strategy,
                "days": days,
                "total_trades": total_trades,
                "win_rate": round(win_rate, 3),
                "total_return_pct": round(total_return_pct, 2),
                "benchmark_return_pct": round(benchmark_return, 2),
                "alpha": round(alpha, 2),
                "sharpe": sharpe,
                "max_drawdown_pct": max_dd,
            },
        }
        self.db.rpush("notifications", json.dumps(notification))
        log.info(
            f"Backtester: Run {run_id} complete — "
            f"{total_trades} trades | WR={win_rate*100:.0f}% | "
            f"Return={total_return_pct:+.1f}% | Alpha={alpha:+.1f}% | Sharpe={sharpe}"
        )

    def run(self):
        log.info("Backtester: Listening for backtest_request on Redis...")
        while True:
            try:
                result = self.db.blpop('backtest_request', timeout=30)
                if result:
                    _, payload = result
                    try:
                        request = json.loads(payload)
                        log.info(f"Backtester: Received request: {request}")
                        self.process_request(request)
                    except Exception as e:
                        log.error(f"Backtester: Request processing failed: {e}")
            except Exception as e:
                log.error(f"Backtester: blpop error: {e}")
                time.sleep(5)


if __name__ == "__main__":
    backtester = Backtester()
    backtester.run()
