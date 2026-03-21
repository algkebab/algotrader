import json
import math
import os
import sys
import time

import ccxt
import redis
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# Allow importing shared.db
_this_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.abspath(os.path.join(_this_dir, "..", "..")) if os.path.basename(_this_dir) in ("executor", "monitor") else _this_dir
if _root not in sys.path:
    sys.path.insert(0, _root)

from shared import config as shared_config
from shared import db as shared_db
from shared import logger as shared_logger

log = shared_logger.get_logger("monitor")


class Monitor:
    def __init__(self):
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        self.exchange = ccxt.binance({
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })
        # MFE/MAE tracking: {order_id: {'mfe': float, 'mae': float}}
        # Populated from DB on startup (restart recovery) and updated each price tick.
        self._extremes: dict = {}

    def _get_positions_to_monitor(self):
        """Returns list of (symbol, trade_dict) from DB (paper mode only)."""
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                rows = shared_db.get_open_orders(conn)
            return [
                (r["symbol"], {
                    "id": r["id"],
                    "entry": float(r["entry_price"]),
                    "qty": float(r["quantity"]),
                    "tp": float(r["tp_price"]),
                    "sl": float(r["sl_price"]),
                    "opened_at": r["opened_at"],
                    "mfe_pct": r.get("mfe_pct"),
                    "mae_pct": r.get("mae_pct"),
                })
                for r in rows
            ]
        except Exception as e:
            log.error(f"Monitor: DB error: {e}")
            return []

    def run(self):
        log.info("Monitor: Tracking active positions...")
        while True:
            positions = self._get_positions_to_monitor()
            if not positions:
                time.sleep(5)
                continue

            symbols = [s for s, _ in positions]
            try:
                tickers = self.exchange.fetch_tickers(symbols)
            except Exception as e:
                log.error(f"Monitor: fetch_tickers error: {e}")
                tickers = {}

            for symbol, trade in positions:
                try:
                    ticker = tickers.get(symbol)
                    if not ticker:
                        ticker = self.exchange.fetch_ticker(symbol)
                    current_price = ticker['last']
                    entry_price = trade['entry']
                    sl_price = trade['sl']
                    tp_price = trade['tp']
                    order_id = trade['id']

                    # Seed MFE/MAE from DB on first sight of this order (restart recovery)
                    if order_id not in self._extremes:
                        self._extremes[order_id] = {
                            'mfe': trade.get('mfe_pct') or 0.0,
                            'mae': trade.get('mae_pct') or 0.0,
                        }

                    # Update MFE/MAE with current unrealized PnL %
                    current_pnl_pct = (current_price - entry_price) / entry_price * 100
                    prev = self._extremes[order_id].copy()
                    self._extremes[order_id]['mfe'] = max(prev['mfe'], current_pnl_pct)
                    self._extremes[order_id]['mae'] = min(prev['mae'], current_pnl_pct)
                    new_mfe = self._extremes[order_id]['mfe']
                    new_mae = self._extremes[order_id]['mae']
                    # Write to DB when extremes shift by >=0.1% to throttle writes
                    if abs(new_mfe - prev['mfe']) >= 0.1 or abs(new_mae - prev['mae']) >= 0.1:
                        try:
                            with shared_db.get_connection() as conn:
                                shared_db.init_schema(conn)
                                shared_db.update_order_extremes(
                                    conn, order_id, round(new_mfe, 3), round(new_mae, 3)
                                )
                        except Exception as e:
                            log.warning(f"Monitor: Failed to persist extremes for {symbol}: {e}")

                    # Time-stop: close positions open longer than 48 hours (paper only)
                    try:
                        opened_at = datetime.fromisoformat(trade["opened_at"].replace("Z", "+00:00"))
                        hours_open = (datetime.now(timezone.utc) - opened_at).total_seconds() / 3600.0
                        if hours_open >= 48:
                            self.close_position(symbol, current_price, "TIME-STOP")
                            continue
                    except Exception:
                        pass

                    # Trailing stop-loss: trail distance = original SL% from entry.
                    # SL only ever moves up — if price hasn't risen, nothing changes.
                    # Only write to DB when new SL is at least 0.1% higher (reduces writes).
                    trail_pct = (entry_price - sl_price) / entry_price * 100
                    if trail_pct > 0:
                        new_sl = current_price * (1 - trail_pct / 100)
                        if new_sl > sl_price * 1.001:
                            try:
                                with shared_db.get_connection() as conn:
                                    shared_db.init_schema(conn)
                                    shared_db.update_order_sl_price(conn, order_id, new_sl)
                                log.info(
                                    f"Monitor: Trailing SL {symbol}: "
                                    f"{sl_price:.6g} -> {new_sl:.6g} "
                                    f"(price={current_price:.6g}, trail={trail_pct:.2f}%)"
                                )
                                sl_price = new_sl  # use updated value for the check below
                            except Exception as e:
                                log.warning(f"Monitor: Failed to update trailing SL for {symbol}: {e}")

                    if current_price <= sl_price:
                        self.close_position(symbol, current_price, "STOP-LOSS")
                    elif current_price >= tp_price:
                        self.close_position(symbol, current_price, "TAKE-PROFIT")
                except Exception as e:
                    log.error(f"Monitor: Error monitoring {symbol}: {e}")

            time.sleep(2)

    def _calculate_close_financials(self, row: dict, price: float) -> dict:
        """Calculate all financial metrics for closing a position.

        Extracted from close_position for testability. Returns a flat dict of
        all computed values needed for DB writes and notifications.
        """
        entry_price = float(row["entry_price"])
        qty = float(row["quantity"])
        notional_usdt = float(row["amount_usdt"])
        margin_usdt = notional_usdt / shared_config.LEVERAGE

        gross_pnl_usdt = (price - entry_price) * qty
        gross_pnl_percent = ((price / entry_price) - 1) * 100
        exit_fee_usd = qty * price * shared_config.BINANCE_TAKER_FEE

        borrowed_amount = row.get("borrowed_amount")
        try:
            borrowed_amount = float(borrowed_amount) if borrowed_amount is not None else None
        except (TypeError, ValueError):
            borrowed_amount = None
        if borrowed_amount is None:
            borrowed_amount = max(0.0, notional_usdt - margin_usdt)

        try:
            hourly_rate = float(row.get("hourly_interest_rate"))
        except (TypeError, ValueError):
            hourly_rate = shared_config.HOURLY_MARGIN_INTEREST_RATE

        try:
            opened_at = datetime.fromisoformat(row["opened_at"].replace("Z", "+00:00"))
            hours_held = max(1, math.ceil((datetime.now(timezone.utc) - opened_at).total_seconds() / 3600.0))
        except Exception:
            hours_held = 1

        margin_interest_paid = borrowed_amount * hourly_rate * hours_held
        net_pnl_usdt = gross_pnl_usdt - exit_fee_usd - margin_interest_paid
        net_pnl_pct = (net_pnl_usdt / margin_usdt) * 100 if margin_usdt else 0.0

        return {
            "margin_usdt": margin_usdt,
            "gross_pnl_usdt": gross_pnl_usdt,
            "gross_pnl_percent": gross_pnl_percent,
            "exit_fee_usd": exit_fee_usd,
            "margin_interest_paid": margin_interest_paid,
            "net_pnl_usdt": net_pnl_usdt,
            "net_pnl_pct": net_pnl_pct,
            "hours_held": hours_held,
        }

    def close_position(self, symbol, price, reason):
        """Close position: update DB and notify (paper mode only, DB-backed)."""
        order_id = None
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                row = shared_db.get_open_order_for_symbol(conn, symbol)
                if not row:
                    return
                order_id = row["id"]
                fin = self._calculate_close_financials(row, price)

                log.info(
                    f"Monitor: Closing {symbol} at {price}. "
                    f"Gross PnL: {fin['gross_pnl_usdt']:.2f} USDT ({fin['gross_pnl_percent']:.2f}%), "
                    f"Fees/Interest: {fin['exit_fee_usd'] + fin['margin_interest_paid']:.2f} USDT "
                    f"-> Net PnL: {fin['net_pnl_usdt']:.2f} USDT ({fin['net_pnl_pct']:.2f}%) "
                    f"[{reason}] (paper margin simulation)"
                )

                trade_mfe = self._extremes.get(order_id, {}).get('mfe')
                trade_mae = self._extremes.get(order_id, {}).get('mae')
                shared_db.update_order_closed(
                    conn, order_id,
                    pnl_usdt=round(fin['net_pnl_usdt'], 2),
                    pnl_percent=round(fin['net_pnl_pct'], 2),
                    close_reason=reason,
                    exit_fee_usd=round(fin['exit_fee_usd'], 4),
                    margin_interest_paid=round(fin['margin_interest_paid'], 4),
                    net_pnl_pct=round(fin['net_pnl_pct'], 2),
                    exit_price=float(price),
                    hours_held=float(fin['hours_held']),
                    mfe_pct=round(trade_mfe, 3) if trade_mfe is not None else None,
                    mae_pct=round(trade_mae, 3) if trade_mae is not None else None,
                )

                bal = shared_db.get_balance(conn, "USDT")
                new_balance = (bal + fin['margin_usdt'] + fin['gross_pnl_usdt']
                               - fin['exit_fee_usd'] - fin['margin_interest_paid'])
                shared_db.set_balance(conn, "USDT", new_balance)
                shared_db.record_daily_pnl(conn, round(fin['net_pnl_usdt'], 2))

                signal_id = row.get("signal_id")
                if signal_id:
                    outcome = "WIN" if fin['net_pnl_usdt'] > 0 else ("BREAKEVEN" if fin['net_pnl_usdt'] == 0 else "LOSS")
                    try:
                        shared_db.update_signal_outcome(
                            conn, signal_id=signal_id,
                            outcome=outcome,
                            pnl_usdt=round(fin['net_pnl_usdt'], 2),
                            pnl_pct=round(fin['net_pnl_pct'], 2),
                            close_reason=reason,
                        )
                        log.info(f"Monitor: Signal outcome recorded ({signal_id[:8]}...): {outcome} {fin['net_pnl_usdt']:+.2f} USDT")
                    except Exception as e:
                        log.warning(f"Monitor: Failed to record signal outcome for {symbol}: {e}")

        except Exception as e:
            log.error(f"Monitor: DB error in close_position for {symbol}: {e}")
            return

        self.db.rpush('notifications', json.dumps({
            "type": "trade_closed",
            "data": {
                "symbol": symbol,
                "entry": float(row["entry_price"]),
                "exit": price,
                "pnl_usdt": round(fin['net_pnl_usdt'], 2),
                "pnl_percent": round(fin['net_pnl_pct'], 2),
                "reason": reason,
                "gross_pnl_usdt": round(fin['gross_pnl_usdt'], 2),
                "gross_pnl_percent": round(fin['gross_pnl_percent'], 2),
                "exit_fee_usd": round(fin['exit_fee_usd'], 4),
                "margin_interest_paid": round(fin['margin_interest_paid'], 4),
                "net_pnl_pct": round(fin['net_pnl_pct'], 2),
                "hours_held": fin['hours_held'],
                "strategy_name": row.get("strategy_name"),
            },
        }))
        self._extremes.pop(order_id, None)


if __name__ == "__main__":
    monitor = Monitor()
    monitor.run()
