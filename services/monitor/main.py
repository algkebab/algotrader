import json
import os
import sys
import time
import redis
import ccxt
from datetime import datetime
import math

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

                    # Time-stop: close positions open longer than 48 hours (paper only)
                    try:
                        opened_at = datetime.fromisoformat(trade["opened_at"].replace("Z", ""))
                        hours_open = (datetime.utcnow() - opened_at).total_seconds() / 3600.0
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

    def close_position(self, symbol, price, reason):
        """Close position: update DB and notify (paper mode only, DB-backed)."""
        margin_usdt = 0.0  # add back locked margin on close
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                row = shared_db.get_open_order_for_symbol(conn, symbol)
            if not row:
                return
            entry_price = float(row["entry_price"])
            qty = float(row["quantity"])
            # amount_usdt in DB is notional; margin = notional / leverage
            notional_usdt = float(row["amount_usdt"])
            margin_usdt = notional_usdt / shared_config.LEVERAGE
        except Exception as e:
            log.error(f"Monitor: DB error in close_position: {e}")
            return

        gross_pnl_usdt = (price - entry_price) * qty
        gross_pnl_percent = ((price / entry_price) - 1) * 100

        exit_fee_usd = 0.0
        margin_interest_paid = 0.0
        # Fees and margin interest simulation for paper trades
        exit_notional_usdt = qty * price
        exit_fee_usd = exit_notional_usdt * shared_config.BINANCE_SPOT_FEE

        # Use stored borrowed_amount and hourly_interest_rate from order when present (Executor saves at entry)
        borrowed_amount = row.get("borrowed_amount")
        if borrowed_amount is not None:
            try:
                borrowed_amount = float(borrowed_amount)
            except (TypeError, ValueError):
                borrowed_amount = None
        if borrowed_amount is None:
            effective_total_balance = notional_usdt / shared_config.LEVERAGE
            borrowed_amount = max(0.0, notional_usdt - effective_total_balance)
        hourly_rate = row.get("hourly_interest_rate")
        if hourly_rate is not None:
            try:
                hourly_rate = float(hourly_rate)
            except (TypeError, ValueError):
                hourly_rate = shared_config.HOURLY_MARGIN_INTEREST_RATE
        else:
            hourly_rate = shared_config.HOURLY_MARGIN_INTEREST_RATE

        # Hours held (rounded up as on Binance)
        try:
            opened_at = datetime.fromisoformat(row["opened_at"].replace("Z", ""))
            now = datetime.utcnow()
            hours_held = max(1, math.ceil((now - opened_at).total_seconds() / 3600.0))
        except Exception:
            hours_held = 1

        margin_interest_paid = borrowed_amount * hourly_rate * hours_held
        total_costs = exit_fee_usd + margin_interest_paid
        net_pnl_usdt = gross_pnl_usdt - total_costs
        # Net PnL percent vs margin (ROE after all fees and interest)
        equity_usdt = margin_usdt if margin_usdt else notional_usdt
        net_pnl_pct = (net_pnl_usdt / equity_usdt) * 100 if equity_usdt else 0.0

        log.info(
            f"Monitor: Closing {symbol} at {price}. "
            f"Gross PnL: {gross_pnl_usdt:.2f} USDT ({gross_pnl_percent:.2f}%), "
            f"Fees/Interest: {total_costs:.2f} USDT "
            f"-> Net PnL: {net_pnl_usdt:.2f} USDT ({net_pnl_pct:.2f}%) [{reason}] (paper)"
        )

        # Extra metadata for Messenger (fees, interest, strategy, holding time)
        data_payload = {
            "symbol": symbol,
            "entry": entry_price,
            "exit": price,
            "pnl_usdt": round(net_pnl_usdt, 2),
            "pnl_percent": round(net_pnl_pct, 2),
            "reason": reason,
            "gross_pnl_usdt": round(gross_pnl_usdt, 2),
            "gross_pnl_percent": round(gross_pnl_percent, 2),
            "exit_fee_usd": round(exit_fee_usd, 4),
            "margin_interest_paid": round(margin_interest_paid, 4),
            "net_pnl_pct": round(net_pnl_pct, 2),
            "hours_held": hours_held,
            "strategy_name": row.get("strategy_name"),
        }

        notification = {
            "type": "trade_closed",
            "data": data_payload,
        }
        self.db.rpush('notifications', json.dumps(notification))

        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                order_id = shared_db.get_open_order_id_for_symbol(conn, symbol)
                if order_id is not None:
                    shared_db.update_order_closed(
                        conn,
                        order_id,
                        pnl_usdt=round(net_pnl_usdt, 2),
                        pnl_percent=round(net_pnl_pct, 2),
                        close_reason=reason,
                        exit_fee_usd=float(exit_fee_usd),
                        margin_interest_paid=float(margin_interest_paid),
                        net_pnl_pct=round(net_pnl_pct, 2),
                    )
                    bal = shared_db.get_balance(conn, "USDT")
                    # Paper: new_balance = balance + margin + gross_pnl - exit_fee - interest
                    new_balance = bal + margin_usdt + gross_pnl_usdt - exit_fee_usd - margin_interest_paid
                    shared_db.set_balance(conn, "USDT", new_balance)
        except Exception as db_err:
            log.warning(f"Monitor: DB update failed: {db_err}")

        # Write trade outcome back to the originating AI signal for self-calibration
        signal_id = row.get("signal_id")
        if signal_id:
            outcome = "WIN" if net_pnl_usdt > 0 else ("BREAKEVEN" if net_pnl_usdt == 0 else "LOSS")
            try:
                with shared_db.get_connection() as conn:
                    shared_db.init_schema(conn)
                    shared_db.update_signal_outcome(
                        conn,
                        signal_id=signal_id,
                        outcome=outcome,
                        pnl_usdt=round(net_pnl_usdt, 2),
                        pnl_pct=round(net_pnl_pct, 2),
                        close_reason=reason,
                    )
                log.info(f"Monitor: Signal outcome recorded ({signal_id[:8]}...): {outcome} {net_pnl_usdt:+.2f} USDT")
            except Exception as e:
                log.warning(f"Monitor: Failed to record signal outcome for {symbol}: {e}")


if __name__ == "__main__":
    monitor = Monitor()
    monitor.run()
