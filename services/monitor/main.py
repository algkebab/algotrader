import json
import os
import sys
import time
import redis
import ccxt
from datetime import datetime, timezone

# Allow importing shared.db
_this_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.abspath(os.path.join(_this_dir, "..", "..")) if os.path.basename(_this_dir) in ("executor", "monitor") else _this_dir
if _root not in sys.path:
    sys.path.insert(0, _root)

from shared import db as shared_db


def _ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

# Must match Executor.paper_leverage for balance math (margin = order amount_usdt / this)
PAPER_LEVERAGE = 3


class Monitor:
    def __init__(self):
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)
        self.exchange = ccxt.binance({
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })

    def _is_paper_trading(self):
        """Paper trading ON when key is absent or '1'."""
        val = self.db.get("system:papertrading")
        return val != "0"

    def _get_positions_to_monitor(self):
        """Returns list of (symbol, trade_dict). In paper mode from DB (status=open); in live from Redis."""
        if self._is_paper_trading():
            try:
                with shared_db.get_connection() as conn:
                    shared_db.init_schema(conn)
                    rows = shared_db.get_open_orders(conn)
                return [
                    (r["symbol"], {
                        "entry": float(r["entry_price"]),
                        "qty": float(r["quantity"]),
                        "tp": float(r["tp_price"]),
                        "sl": float(r["sl_price"]),
                    })
                    for r in rows
                ]
            except Exception as e:
                print(f"[{_ts()}] ❌ Monitor: DB error: {e}")
                return []
        trades = self.db.hgetall('active_trades') or {}
        return [(s, json.loads(data)) for s, data in trades.items()]

    def run(self):
        print(f"[{_ts()}] 🛰️ Monitor: Tracking active positions...")
        while True:
            positions = self._get_positions_to_monitor()
            if not positions:
                time.sleep(5)
                continue

            for symbol, trade in positions:
                try:
                    ticker = self.exchange.fetch_ticker(symbol)
                    current_price = ticker['last']
                    entry_price = trade['entry']
                    sl_price = trade['sl']
                    tp_price = trade['tp']

                    if current_price <= sl_price:
                        self.close_position(symbol, current_price, "STOP-LOSS 🔴")
                    elif current_price >= tp_price:
                        self.close_position(symbol, current_price, "TAKE-PROFIT 🟢")
                    else:
                        # Trailing stop: move SL to break-even when 2% above entry (live only; Redis has trade state)
                        if not self._is_paper_trading():
                            trade_json = self.db.hget('active_trades', symbol)
                            if trade_json:
                                t = json.loads(trade_json)
                                if current_price > entry_price * 1.02 and t.get('sl', 0) < entry_price:
                                    t['sl'] = entry_price
                                    self.db.hset('active_trades', symbol, json.dumps(t))
                                    print(f"[{_ts()}] 🛡️ {symbol}: SL moved to BREAK-EVEN")
                except Exception as e:
                    print(f"[{_ts()}] ❌ Error monitoring {symbol}: {e}")

            time.sleep(2)

    def close_position(self, symbol, price, reason):
        """Close position: update DB and notify. In live mode also remove from Redis active_trades."""
        paper = self._is_paper_trading()
        margin_usdt = 0.0  # only used for paper: add back locked margin on close
        if paper:
            try:
                with shared_db.get_connection() as conn:
                    shared_db.init_schema(conn)
                    row = shared_db.get_open_order_for_symbol(conn, symbol)
                if not row:
                    return
                entry_price = float(row["entry_price"])
                qty = float(row["quantity"])
                # amount_usdt in DB is notional; margin = notional / leverage
                margin_usdt = float(row["amount_usdt"]) / PAPER_LEVERAGE
            except Exception as e:
                print(f"[{_ts()}] ❌ Monitor: DB error in close_position: {e}")
                return
        else:
            trade_json = self.db.hget('active_trades', symbol)
            if not trade_json:
                return
            trade = json.loads(trade_json)
            entry_price = float(trade['entry'])
            qty = float(trade['qty'])

        pnl_usdt = (price - entry_price) * qty
        pnl_percent = ((price / entry_price) - 1) * 100
        print(f"[{_ts()}] 🚩 CLOSING {symbol} at {price}. PnL: {pnl_usdt:.2f} USDT ({pnl_percent:.2f}%)" + (" (paper)" if paper else ""))

        notification = {
            "type": "trade_closed",
            "data": {
                "symbol": symbol,
                "entry": entry_price,
                "exit": price,
                "pnl_usdt": round(pnl_usdt, 2),
                "pnl_percent": round(pnl_percent, 2),
                "reason": reason
            }
        }
        self.db.rpush('notifications', json.dumps(notification))

        if not paper:
            self.db.hdel('active_trades', symbol)

        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                order_id = shared_db.get_open_order_id_for_symbol(conn, symbol)
                if order_id is not None:
                    shared_db.update_order_closed(
                        conn, order_id,
                        pnl_usdt=round(pnl_usdt, 2),
                        pnl_percent=round(pnl_percent, 2),
                        close_reason=reason,
                    )
                    bal = shared_db.get_balance(conn, "USDT")
                    # Paper: add back locked margin + PnL; live: only PnL
                    shared_db.set_balance(conn, "USDT", bal + pnl_usdt + margin_usdt)
        except Exception as db_err:
            print(f"[{_ts()}] ⚠️ DB update failed: {db_err}")

if __name__ == "__main__":
    monitor = Monitor()
    monitor.run()