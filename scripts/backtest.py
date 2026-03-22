#!/usr/bin/env python3
"""
Backtest runner for algotrader.

Fetches historical OHLCV from Binance public API (no auth required),
runs portfolio-level simulation using the code decision engine, and
writes results to backtest.db (never touches live algotrader.db).

Usage:
    python scripts/backtest.py --run-id <uuid> --strategy CONSERVATIVE --balance 1000

Args:
    --run-id    UUID created by the dashboard (required)
    --strategy  CONSERVATIVE | AGGRESSIVE | REVERSAL (default: CONSERVATIVE)
    --balance   Starting USDT balance (default: 1000)
"""

import argparse
import bisect
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

# Allow importing shared (from scripts/ subdirectory or /app in Docker)
_this_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.abspath(os.path.join(_this_dir, ".."))
if _root not in sys.path:
    sys.path.insert(0, _root)

from shared import backtest_db
from shared import decision as decision_engine
from shared.version import BOT_VERSION

# ── Constants ─────────────────────────────────────────────────────────────────

BINANCE_BASE = "https://api.binance.com"
KLINES_LIMIT = 1000          # max bars per Binance request
RATE_LIMIT_SLEEP = 0.12      # ~8 req/s (Binance allows 1200 req/min)
WARMUP_DAYS = 20             # bars to skip at start for indicator warmup (EMA50 + ADX)
MAX_SYMBOLS = 30             # top N symbols by 24h volume
MAX_OPEN_POSITIONS = 4       # concurrent positions (mirrors live setting)
LEVERAGE = 3                 # matches live config
TAKER_FEE = 0.001            # 0.1% per side
POSITION_RISK_PCT = 0.01     # 1% of balance per position
TIME_STOP_HOURS = 24         # close after 24h (matches live monitor)
ATR_TRAILING_MULTIPLIER = 2.0

TIMEFRAMES = ["15m", "1h", "4h"]
TF_MS = {"15m": 15 * 60 * 1000, "1h": 60 * 60 * 1000, "4h": 4 * 60 * 60 * 1000}

# ── Binance API helpers ────────────────────────────────────────────────────────

def _fetch_top_symbols(n: int) -> list[str]:
    """Fetch top N USDT perpetual symbols by 24h quote volume."""
    resp = requests.get(f"{BINANCE_BASE}/api/v3/ticker/24hr", timeout=30)
    resp.raise_for_status()
    tickers = resp.json()
    usdt = [
        t for t in tickers
        if t["symbol"].endswith("USDT")
        and not t["symbol"].endswith("DOWNUSDT")
        and not t["symbol"].endswith("UPUSDT")
        and not t["symbol"].endswith("BULLUSDT")
        and not t["symbol"].endswith("BEARUSDT")
        and float(t.get("quoteVolume", 0)) > 1_000_000
    ]
    usdt.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
    # Filter out stablecoins
    stables = {"USDC", "BUSD", "TUSD", "DAI", "USDT", "FDUSD", "USDP"}
    result = []
    for t in usdt:
        base = t["symbol"][:-4]
        if base not in stables and len(result) < n:
            result.append(t["symbol"])
    return result


def _fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list:
    """Fetch all klines in [start_ms, end_ms] by paginating Binance API."""
    candles = []
    current_start = start_ms
    while current_start < end_ms:
        url = (
            f"{BINANCE_BASE}/api/v3/klines"
            f"?symbol={symbol}&interval={interval}"
            f"&startTime={current_start}&endTime={end_ms}&limit={KLINES_LIMIT}"
        )
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                batch = resp.json()
                break
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        if not batch:
            break
        for c in batch:
            candles.append([
                int(c[0]),    # timestamp ms
                float(c[1]),  # open
                float(c[2]),  # high
                float(c[3]),  # low
                float(c[4]),  # close
                float(c[5]),  # volume
            ])
        if len(batch) < KLINES_LIMIT:
            break
        current_start = int(batch[-1][0]) + TF_MS[interval]
        time.sleep(RATE_LIMIT_SLEEP)
    return candles


# ── Indicator computation ──────────────────────────────────────────────────────
# All indicator functions operate only on candles UP TO the current bar
# (look-ahead bias prevention is enforced by the caller via bisect).

def _compute_ema(prices: list, period: int) -> list:
    if len(prices) < period:
        return []
    k = 2.0 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for p in prices[period:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return ema


def _compute_ema_stack(closes: list) -> dict | None:
    ema9 = _compute_ema(closes, 9)
    ema21 = _compute_ema(closes, 21)
    ema50 = _compute_ema(closes, 50)
    if not ema9 or not ema21 or not ema50:
        return None
    e9, e21, e50 = ema9[-1], ema21[-1], ema50[-1]
    if e9 > e21 > e50:
        alignment, desc = "BULLISH", "EMA9 > EMA21 > EMA50 (full bullish stack)"
    elif e9 < e21 < e50:
        alignment, desc = "BEARISH", "EMA9 < EMA21 < EMA50 (full bearish stack)"
    elif e9 > e21 and e21 < e50:
        alignment, desc = "RECOVERING", "EMA9 > EMA21 < EMA50 (short-term recovery)"
    elif e9 < e21 and e21 > e50:
        alignment, desc = "WEAKENING", "EMA9 < EMA21 > EMA50 (short-term weakening)"
    else:
        alignment, desc = "MIXED", "Mixed EMA stack"
    return {"ema9": e9, "ema21": e21, "ema50": e50, "alignment": alignment, "description": desc}


def _compute_macd(closes: list) -> dict | None:
    ema_fast = _compute_ema(closes, 12)
    ema_slow = _compute_ema(closes, 26)
    if not ema_fast or not ema_slow:
        return None
    offset = len(ema_fast) - len(ema_slow)
    macd_line = [ema_fast[i + offset] - ema_slow[i] for i in range(len(ema_slow))]
    signal_ema = _compute_ema(macd_line, 9)
    if not signal_ema:
        return None
    hist = macd_line[-1] - signal_ema[-1]
    hist_prev = (macd_line[-2] - signal_ema[-2]) if len(signal_ema) >= 2 else None
    return {"histogram": round(hist, 8), "histogram_prev": hist_prev}


def _compute_rsi(candles: list, period: int = 14) -> float:
    if len(candles) < period + 1:
        return 50.0
    closes = [float(c[4]) for c in candles]
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_g / avg_l)), 2)


def _compute_rvol(candles: list, period: int = 50) -> float:
    if len(candles) < period + 2:
        return 0.0
    current_vol = candles[-2][5]
    avg_vol = sum(c[5] for c in candles[-(period + 2):-2]) / period
    return round(current_vol / avg_vol, 2) if avg_vol > 0 else 0.0


def _compute_atr(candles: list, period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i][2], candles[i][3], candles[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _compute_bollinger(candles: list, period: int = 20) -> dict | None:
    if len(candles) < period:
        return None
    closes = [float(c[4]) for c in candles]
    window = closes[-period:]
    mid = sum(window) / period
    std = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    upper = mid + 2 * std
    lower = mid - 2 * std
    band_range = upper - lower
    pct_b = (closes[-1] - lower) / band_range * 100 if band_range > 0 else 50.0
    bw = band_range / mid * 100 if mid > 0 else 0.0
    return {"pct_b": round(pct_b, 1), "bandwidth": round(bw, 2)}


def _compute_vwap(candles: list) -> float | None:
    if not candles:
        return None
    tv = sum((c[2] + c[3] + c[4]) / 3.0 * c[5] for c in candles)
    vol = sum(c[5] for c in candles)
    return tv / vol if vol > 0 else None


def _compute_indicators(candles_15m: list, candles_1h: list, candles_4h: list) -> dict:
    """Compute all indicators needed by the decision engine."""
    ind = {}
    if candles_15m:
        closes = [float(c[4]) for c in candles_15m]
        ind["ema_stack_15m"] = _compute_ema_stack(closes)
        ind["macd_15m"] = _compute_macd(closes)
        ind["bollinger_15m"] = _compute_bollinger(candles_15m)
        ind["atr"] = _compute_atr(candles_15m)
        # Session VWAP: use today's candles (last 32 as fallback)
        ind["vwap"] = _compute_vwap(candles_15m[-32:])
    if candles_1h:
        closes_1h = [float(c[4]) for c in candles_1h]
        ind["ema_stack_1h"] = _compute_ema_stack(closes_1h)
    if candles_4h:
        closes_4h = [float(c[4]) for c in candles_4h]
        ind["ema_stack_4h"] = _compute_ema_stack(closes_4h)
    return ind


# ── Portfolio simulation ────────────────────────────────────────────────────────

class Position:
    __slots__ = [
        "symbol", "entry_price", "sl_price", "tp_price",
        "quantity", "amount_usdt", "entry_ts", "atr_at_entry",
    ]
    def __init__(self, symbol, entry_price, sl_price, tp_price,
                 quantity, amount_usdt, entry_ts, atr_at_entry=None):
        self.symbol = symbol
        self.entry_price = entry_price
        self.sl_price = sl_price
        self.tp_price = tp_price
        self.quantity = quantity
        self.amount_usdt = amount_usdt
        self.entry_ts = entry_ts
        self.atr_at_entry = atr_at_entry


def _position_size(balance: float, price: float) -> tuple[float, float]:
    """Return (quantity, notional_usdt). Uses 1% of balance * leverage."""
    risk = balance * POSITION_RISK_PCT * LEVERAGE
    quantity = risk / price
    return quantity, risk


def _simulate(
    symbols: list[str],
    candle_data: dict,
    strategy: str,
    initial_balance: float,
    start_ts: int,
    end_ts: int,
    run_id: str,
    progress_callback=None,
) -> list[dict]:
    """Core portfolio simulation. Returns list of trade dicts."""
    # Build sorted timestamp index for 15m
    all_ts_set = set()
    for sym in symbols:
        data_15m = candle_data.get(sym, {}).get("15m", [])
        for c in data_15m:
            ts = int(c[0])
            if start_ts <= ts <= end_ts:
                all_ts_set.add(ts)

    all_timestamps = sorted(all_ts_set)
    if not all_timestamps:
        return []

    balance = initial_balance
    open_positions: dict[str, Position] = {}
    trades = []

    # Pre-build sorted timestamp lists per symbol+timeframe for bisect
    ts_index = {}
    for sym in symbols:
        ts_index[sym] = {}
        for tf in TIMEFRAMES:
            candles = candle_data.get(sym, {}).get(tf, [])
            ts_index[sym][tf] = [int(c[0]) for c in candles]

    total_steps = len(all_timestamps)

    for step_i, ts in enumerate(all_timestamps):
        if progress_callback and step_i % 500 == 0:
            pct = round(step_i / total_steps * 100, 1)
            progress_callback(pct)

        ts_dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)

        # ── 1. Check open positions ─────────────────────────────────────────────
        closed_symbols = []
        for sym, pos in open_positions.items():
            tf_data = candle_data.get(sym, {}).get("15m", [])
            sym_ts_list = ts_index[sym]["15m"]
            # Get current bar index
            idx = bisect.bisect_right(sym_ts_list, ts) - 1
            if idx < 0 or idx >= len(tf_data):
                continue
            bar = tf_data[idx]
            high, low = float(bar[2]), float(bar[3])
            close = float(bar[4])

            # Time stop
            hours_open = (ts - pos.entry_ts) / (3600 * 1000)
            if hours_open >= TIME_STOP_HOURS:
                pnl = _calc_pnl(pos, close, balance)
                trades.append(_make_trade(pos, close, ts_dt, pnl, balance, strategy, "TIME-STOP"))
                balance += pnl["usdt"]
                closed_symbols.append(sym)
                continue

            # ATR trailing stop update
            if idx >= 14:
                recent_bars = tf_data[max(0, idx - 50):idx + 1]
                atr_now = _compute_atr(recent_bars)
                if atr_now:
                    new_sl = close - atr_now * ATR_TRAILING_MULTIPLIER
                    if new_sl > pos.sl_price:
                        pos.sl_price = new_sl

            # Stop-loss hit
            if low <= pos.sl_price:
                exit_price = pos.sl_price  # realistic fill at SL
                pnl = _calc_pnl(pos, exit_price, balance)
                trades.append(_make_trade(pos, exit_price, ts_dt, pnl, balance, strategy, "STOP-LOSS"))
                balance += pnl["usdt"]
                closed_symbols.append(sym)
                continue

            # Take-profit hit
            if high >= pos.tp_price:
                exit_price = pos.tp_price
                pnl = _calc_pnl(pos, exit_price, balance)
                trades.append(_make_trade(pos, exit_price, ts_dt, pnl, balance, strategy, "TAKE-PROFIT"))
                balance += pnl["usdt"]
                closed_symbols.append(sym)
                continue

        for sym in closed_symbols:
            del open_positions[sym]

        # ── 2. Analyze new candidates ────────────────────────────────────────────
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            continue

        for sym in symbols:
            if sym in open_positions:
                continue
            if len(open_positions) >= MAX_OPEN_POSITIONS:
                break

            sym_data = candle_data.get(sym, {})
            sym_ts_15m = ts_index[sym]["15m"]
            sym_ts_1h = ts_index[sym]["1h"]
            sym_ts_4h = ts_index[sym]["4h"]

            # Get candles up to (but not including) current timestamp
            idx_15m = bisect.bisect_right(sym_ts_15m, ts) - 1
            idx_1h = bisect.bisect_right(sym_ts_1h, ts) - 1
            idx_4h = bisect.bisect_right(sym_ts_4h, ts) - 1

            if idx_15m < 60:  # need enough bars for indicators
                continue

            candles_15m = sym_data["15m"][:idx_15m + 1][-120:]
            candles_1h = sym_data["1h"][:idx_1h + 1][-50:] if idx_1h >= 0 else []
            candles_4h = sym_data["4h"][:idx_4h + 1][-100:] if idx_4h >= 0 else []

            price = float(candles_15m[-1][4])
            rsi = _compute_rsi(candles_15m)
            rvol = _compute_rvol(candles_15m)

            # Filter gates (mirrors live filter logic)
            ema_4h = _compute_ema_stack([float(c[4]) for c in candles_4h]) if candles_4h else None
            if ema_4h and ema_4h["alignment"] not in ("BULLISH", "RECOVERING"):
                continue  # 4h EMA mandatory gate

            indicators = _compute_indicators(candles_15m, candles_1h, candles_4h)

            # Strategy-specific quick pre-filter (saves decision engine calls)
            if strategy == "CONSERVATIVE" and not (40 <= rsi <= 70):
                continue
            if strategy == "AGGRESSIVE" and rvol < 1.2:
                continue
            if strategy == "REVERSAL" and rsi >= 35:
                continue

            analysis, sig_id = decision_engine.make_decision(
                symbol=sym,
                price=price,
                rsi=rsi,
                rvol=rvol,
                candles_15m=candles_15m[-30:],
                candles_1h=candles_1h[-24:],
                indicators=indicators,
                strategy=strategy,
            )

            if analysis["verdict"] != "BUY":
                continue

            sl_pct = analysis["stop_loss_pct"]
            tp_pct = analysis["take_profit_pct"]
            if not sl_pct or not tp_pct:
                continue

            sl_price = price * (1 - sl_pct / 100)
            tp_price = price * (1 + tp_pct / 100)
            quantity, amount_usdt = _position_size(balance, price)

            entry_fee = amount_usdt * TAKER_FEE
            balance -= entry_fee

            open_positions[sym] = Position(
                symbol=sym,
                entry_price=price,
                sl_price=sl_price,
                tp_price=tp_price,
                quantity=quantity,
                amount_usdt=amount_usdt,
                entry_ts=ts,
                atr_at_entry=indicators.get("atr"),
            )

    # Force-close remaining open positions at last known price
    for sym, pos in open_positions.items():
        sym_data = candle_data.get(sym, {})
        candles = sym_data.get("15m", [])
        if candles:
            close = float(candles[-1][4])
            close_dt = datetime.fromtimestamp(
                int(candles[-1][0]) / 1000, tz=timezone.utc
            )
            pnl = _calc_pnl(pos, close, balance)
            trades.append(_make_trade(pos, close, close_dt, pnl, balance, strategy, "FORCED-CLOSE"))
            balance += pnl["usdt"]

    return trades, balance


def _calc_pnl(pos: Position, exit_price: float, balance: float) -> dict:
    """Calculate P&L for closing a position."""
    gross_pnl = (exit_price - pos.entry_price) / pos.entry_price * pos.amount_usdt * LEVERAGE
    exit_fee = pos.amount_usdt * TAKER_FEE
    net_pnl = gross_pnl - exit_fee
    pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * 100
    return {"usdt": round(net_pnl, 4), "pct": round(pnl_pct, 4)}


def _make_trade(pos: Position, exit_price: float, exit_dt: datetime,
                pnl: dict, balance: float, strategy: str, reason: str) -> dict:
    entry_dt = datetime.fromtimestamp(pos.entry_ts / 1000, tz=timezone.utc)
    hours_held = (pos.entry_ts - pos.entry_ts + (exit_dt.timestamp() - entry_dt.timestamp())) / 3600
    hours_held = round((exit_dt.timestamp() - entry_dt.timestamp()) / 3600, 2)
    return {
        "symbol": pos.symbol,
        "entry_price": pos.entry_price,
        "exit_price": exit_price,
        "sl_price": pos.sl_price,
        "tp_price": pos.tp_price,
        "entry_time": entry_dt.isoformat(),
        "exit_time": exit_dt.isoformat(),
        "hours_held": hours_held,
        "pnl_usdt": pnl["usdt"],
        "pnl_pct": pnl["pct"],
        "close_reason": reason,
        "strategy": strategy,
    }


def _compute_metrics(trades: list, initial_balance: float, final_balance: float) -> dict:
    """Compute summary metrics from a list of trade dicts."""
    if not trades:
        return {
            "trades_count": 0, "win_count": 0, "win_rate": 0,
            "profit_factor": None, "sharpe": None, "max_drawdown": 0,
            "final_balance": round(final_balance, 2), "avg_hold_hours": None,
        }
    wins = [t for t in trades if t["pnl_usdt"] > 0]
    losses = [t for t in trades if t["pnl_usdt"] <= 0]
    gross_wins = sum(t["pnl_usdt"] for t in wins)
    gross_losses = abs(sum(t["pnl_usdt"] for t in losses))
    pf = round(gross_wins / gross_losses, 2) if gross_losses > 0 else None

    # Sharpe approximation: group trades by day
    from collections import defaultdict
    daily = defaultdict(float)
    for t in trades:
        day = t["exit_time"][:10]
        daily[day] += t["pnl_usdt"]
    pnls = list(daily.values())
    sharpe = None
    if len(pnls) >= 5:
        mean_p = statistics.mean(pnls)
        std_p = statistics.stdev(pnls)
        sharpe = round(mean_p / std_p * math.sqrt(365), 2) if std_p > 0 else None

    # Max drawdown
    balance = initial_balance
    peak = initial_balance
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x["exit_time"]):
        balance += t["pnl_usdt"]
        if balance > peak:
            peak = balance
        dd = peak - balance
        if dd > max_dd:
            max_dd = dd

    holds = [t["hours_held"] for t in trades if t["hours_held"]]
    avg_hold = round(statistics.mean(holds), 1) if holds else None

    return {
        "trades_count": len(trades),
        "win_count": len(wins),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "profit_factor": pf,
        "sharpe": sharpe,
        "max_drawdown": round(max_dd, 2),
        "final_balance": round(final_balance, 2),
        "avg_hold_hours": avg_hold,
    }


# ── Main entry point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Algotrader backtester")
    parser.add_argument("--run-id", required=True, help="UUID created by dashboard")
    parser.add_argument("--strategy", default="CONSERVATIVE",
                        choices=["CONSERVATIVE", "AGGRESSIVE", "REVERSAL"])
    parser.add_argument("--balance", type=float, default=1000.0,
                        help="Initial USDT balance")
    args = parser.parse_args()

    run_id = args.run_id
    strategy = args.strategy.upper()
    initial_balance = args.balance

    # Date range: yesterday back 365 days
    yesterday = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    end_date = yesterday
    start_date = yesterday - timedelta(days=365)
    start_ts = int(start_date.timestamp() * 1000)
    end_ts = int(end_date.timestamp() * 1000)

    print(f"[backtest] run_id={run_id} strategy={strategy} balance={initial_balance}")
    print(f"[backtest] date range: {start_date.date()} to {end_date.date()}")

    with backtest_db.get_connection() as conn:
        backtest_db.init_schema(conn)
        run = backtest_db.get_run(conn, run_id)
        if not run:
            print(f"[backtest] ERROR: run {run_id} not found in backtest.db", flush=True)
            sys.exit(1)
        backtest_db.update_run_status(conn, run_id, "running_fetch")

    # ── Step 1: Fetch / cache OHLCV ──────────────────────────────────────────
    print("[backtest] Fetching top symbols from Binance...", flush=True)
    try:
        symbols = _fetch_top_symbols(MAX_SYMBOLS)
    except Exception as e:
        with backtest_db.get_connection() as conn:
            backtest_db.init_schema(conn)
            backtest_db.update_run_status(conn, run_id, "error", f"Failed to fetch symbols: {e}")
        sys.exit(1)

    print(f"[backtest] Using {len(symbols)} symbols", flush=True)

    # Convert symbols from Binance format (BTCUSDT) to data key (BTC/USDT)
    symbol_map = {s: s[:-4] + "/USDT" for s in symbols}

    with backtest_db.get_connection() as conn:
        backtest_db.init_schema(conn)
        backtest_db.update_run_progress(conn, run_id, 0, len(symbols), "running_fetch")

    candle_data = {}
    for i, binance_sym in enumerate(symbols):
        display_sym = symbol_map[binance_sym]
        candle_data[display_sym] = {}
        print(f"[backtest] Fetching {display_sym} ({i+1}/{len(symbols)})...", flush=True)

        for tf in TIMEFRAMES:
            with backtest_db.get_connection() as conn:
                backtest_db.init_schema(conn)
                if backtest_db.has_candles(conn, display_sym, tf, start_ts, end_ts):
                    candle_data[display_sym][tf] = backtest_db.load_candles(conn, display_sym, tf)
                    print(f"  {tf}: {len(candle_data[display_sym][tf])} bars (cached)", flush=True)
                    continue

            # Need to fetch from Binance
            # Add warmup period (20 days before start)
            warmup_ms = WARMUP_DAYS * 24 * 60 * 60 * 1000
            fetch_start = start_ts - warmup_ms
            try:
                candles = _fetch_klines(binance_sym, tf, fetch_start, end_ts)
                print(f"  {tf}: {len(candles)} bars (fetched)", flush=True)
                if candles:
                    with backtest_db.get_connection() as conn:
                        backtest_db.init_schema(conn)
                        backtest_db.store_candles(conn, display_sym, tf, candles)
                    candle_data[display_sym][tf] = [(c[0], c[1], c[2], c[3], c[4], c[5]) for c in candles]
                else:
                    candle_data[display_sym][tf] = []
            except Exception as e:
                print(f"  {tf}: ERROR fetching — {e}", flush=True)
                candle_data[display_sym][tf] = []

        with backtest_db.get_connection() as conn:
            backtest_db.init_schema(conn)
            backtest_db.update_run_progress(conn, run_id, i + 1, len(symbols), "running_fetch")

    # ── Step 2: Simulation ────────────────────────────────────────────────────
    print("[backtest] Starting simulation...", flush=True)
    with backtest_db.get_connection() as conn:
        backtest_db.init_schema(conn)
        backtest_db.update_run_status(conn, run_id, "running_sim")

    # Simulation warmup: skip first 20 days so EMA indicators stabilize
    sim_start_ts = start_ts + WARMUP_DAYS * 24 * 60 * 60 * 1000
    display_symbols = list(symbol_map.values())

    def _progress_cb(pct):
        with backtest_db.get_connection() as conn:
            backtest_db.init_schema(conn)
            conn.execute(
                "UPDATE backtest_runs SET progress_pct=? WHERE id=?",
                (50 + pct * 0.5, run_id)  # 50–100% for simulation phase
            )

    try:
        trades, final_balance = _simulate(
            symbols=display_symbols,
            candle_data=candle_data,
            strategy=strategy,
            initial_balance=initial_balance,
            start_ts=sim_start_ts,
            end_ts=end_ts,
            run_id=run_id,
            progress_callback=_progress_cb,
        )
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        with backtest_db.get_connection() as conn:
            backtest_db.init_schema(conn)
            backtest_db.update_run_status(conn, run_id, "error", f"Simulation error: {e}\n{tb[:500]}")
        sys.exit(1)

    print(f"[backtest] Simulation complete: {len(trades)} trades, final balance {final_balance:.2f}", flush=True)

    # ── Step 3: Persist results ───────────────────────────────────────────────
    metrics = _compute_metrics(trades, initial_balance, final_balance)

    with backtest_db.get_connection() as conn:
        backtest_db.init_schema(conn)
        for t in trades:
            backtest_db.insert_trade(
                conn, run_id=run_id,
                symbol=t["symbol"],
                entry_price=t["entry_price"],
                exit_price=t["exit_price"],
                sl_price=t["sl_price"],
                tp_price=t["tp_price"],
                entry_time=t["entry_time"],
                exit_time=t["exit_time"],
                hours_held=t["hours_held"],
                pnl_usdt=t["pnl_usdt"],
                pnl_pct=t["pnl_pct"],
                close_reason=t["close_reason"],
                strategy=t["strategy"],
                bot_version=BOT_VERSION,
            )
        backtest_db.update_run_metrics(
            conn, run_id=run_id,
            trades_count=metrics["trades_count"],
            win_count=metrics["win_count"],
            profit_factor=metrics["profit_factor"],
            sharpe=metrics["sharpe"],
            max_drawdown=metrics["max_drawdown"],
            final_balance=metrics["final_balance"],
            initial_balance=initial_balance,
            avg_hold_hours=metrics["avg_hold_hours"],
        )
        backtest_db.update_run_status(conn, run_id, "completed")

    print("[backtest] Done.", flush=True)
    print(f"  Trades:  {metrics['trades_count']}", flush=True)
    print(f"  Win rate: {metrics['win_rate']}%", flush=True)
    print(f"  P/F:     {metrics['profit_factor']}", flush=True)
    print(f"  Net PnL: {metrics['final_balance'] - initial_balance:.2f} USDT", flush=True)
    print(f"  Sharpe:  {metrics['sharpe']}", flush=True)
    print(f"  Max DD:  {metrics['max_drawdown']:.2f} USDT", flush=True)


if __name__ == "__main__":
    main()
