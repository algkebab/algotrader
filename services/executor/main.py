import json
import os
import sys
import time
import redis
import ccxt
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# Allow importing shared.db (project root or /app in Docker)
_this_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.abspath(os.path.join(_this_dir, "..", "..")) if os.path.basename(_this_dir) in ("executor", "monitor") else _this_dir
if _root not in sys.path:
    sys.path.insert(0, _root)

from shared import config as shared_config
from shared import db as shared_db
from shared import logger as shared_logger

log = shared_logger.get_logger("executor")


def get_trading_session():
    """Return current trading session(s) based on UTC time.
    ASIA 00-09 UTC, EUROPE 08-17 UTC, NORTH_AMERICA 13-22 UTC.
    Overlaps return e.g. 'EUROPE/NORTH_AMERICA'; else 'LATE_NIGHT'."""
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    sessions = []
    if 0 <= hour < 9:
        sessions.append("ASIA")
    if 8 <= hour < 17:
        sessions.append("EUROPE")
    if 13 <= hour < 22:
        sessions.append("NORTH_AMERICA")
    return "/".join(sessions) if sessions else "LATE_NIGHT"


class Executor:
    def __init__(self):
        # Redis setup
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        self.db = redis.Redis(host=redis_host, port=6379, decode_responses=True)

        # Binance API setup
        self.exchange = ccxt.binance({
            'apiKey': os.getenv('BINANCE_API_KEY'),
            'secret': os.getenv('BINANCE_SECRET'),
            'enableRateLimit': True,
            'options': {'defaultType': 'margin'}
        })

        # Activation of Sandbox (Binance Margin Testnet)
        if os.getenv('IS_TESTNET', 'true').lower() == 'true':
            self.exchange.set_sandbox_mode(True)
            log.warning("Executor: Running in BINANCE MARGIN TESTNET mode")
        else:
            log.info("Executor: Running in BINANCE REAL MARGIN mode")

        self.exchange.load_markets()
        log.info("Executor: Markets loaded")

    def get_precision_amount(self, symbol, amount):
        """Adjusts the coin amount to the exchange's required precision."""
        self.exchange.load_markets()
        return float(self.exchange.amount_to_precision(symbol, amount))

    def get_precision_price(self, symbol, price):
        """Adjusts the price to the exchange's required precision."""
        self.exchange.load_markets()
        return float(self.exchange.price_to_precision(symbol, price))

    def can_open_position(self, symbol):
        """Checks if we already have an active trade for this symbol in paper mode (DB only)."""
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                return shared_db.get_open_order_id_for_symbol(conn, symbol) is None
        except Exception:
            return True

    def _apply_risk_guard(self, symbol, stop_loss_pct, take_profit_pct, signal_id=None):
        """Apply RiskGuard caps and RR adjustments. Returns (adjusted_sl, adjusted_tp)."""
        original_sl = stop_loss_pct
        original_tp = take_profit_pct
        adjusted_sl = stop_loss_pct
        adjusted_tp = take_profit_pct
        adjustments = []

        # Cap SL at MAX_ALLOWED_SL (and set when missing/invalid)
        if adjusted_sl is None or adjusted_sl <= 0 or adjusted_sl > shared_config.RISK_GUARD_MAX_SL:
            adjusted_sl = shared_config.RISK_GUARD_MAX_SL
            adjustments.append(
                f"SL capped at {shared_config.RISK_GUARD_MAX_SL:.2f}% "
                f"(was {original_sl if original_sl is not None else 'None'})"
            )

        # Enforce minimum risk/reward ratio
        if adjusted_tp is None or adjusted_tp <= 0 or adjusted_sl <= 0 or (adjusted_tp / adjusted_sl) < shared_config.RISK_GUARD_MIN_RR:
            new_tp = adjusted_sl * shared_config.RISK_GUARD_MIN_RR
            adjustments.append(
                f"TP set to {new_tp:.2f}% to keep RR={shared_config.RISK_GUARD_MIN_RR:.2f} "
                f"(was {adjusted_tp if adjusted_tp is not None else 'None'})"
            )
            adjusted_tp = new_tp

        if adjustments:
            joined = "; ".join(adjustments)
            log.info(f"Executor: RiskGuard for {symbol} (signal_id={signal_id}): {joined}")
            # Push notification so Messenger can surface this adjustment
            try:
                self.db.rpush(
                    "notifications",
                    json.dumps({
                        "type": "risk_guard_adjustment",
                        "data": {
                            "symbol": symbol,
                            "signal_id": signal_id,
                            "original_stop_loss_pct": original_sl,
                            "adjusted_stop_loss_pct": adjusted_sl,
                            "original_take_profit_pct": original_tp,
                            "adjusted_take_profit_pct": adjusted_tp,
                            "max_allowed_sl": shared_config.RISK_GUARD_MAX_SL,
                            "min_rr_ratio": shared_config.RISK_GUARD_MIN_RR,
                        },
                    }),
                )
            except Exception as e:
                log.warning(f"Executor: RiskGuard notification failed for {symbol}: {e}")

        return adjusted_sl, adjusted_tp

    def place_smart_order(self, symbol, stop_loss_pct=None, take_profit_pct=None, strategy_name=None, signal_id=None):
        """Writes paper order to DB only (no live trading).

        RiskGuard caps stop-loss and enforces a minimum risk/reward ratio
        before the existing paper-trading and DB logic runs.
        """
        try:
            log.info(f"Executor: Processing paper order for {symbol} (signal_id={signal_id})")

            if not self.can_open_position(symbol):
                log.warning(f"Executor: Already monitoring {symbol}. Skipping.")
                self.db.rpush("notifications", json.dumps({
                    "type": "trade_skipped",
                    "data": {"symbol": symbol, "reason": "Already have open order for this symbol"},
                }))
                return {"status": "error", "msg": "Position exists"}

            # Apply RiskGuard before sizing and placement
            guarded_sl, guarded_tp = self._apply_risk_guard(
                symbol,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                signal_id=signal_id,
            )

            return self._place_paper_order(
                symbol,
                stop_loss_pct=guarded_sl,
                take_profit_pct=guarded_tp,
                strategy_name=strategy_name,
                signal_id=signal_id,
            )

        except Exception as e:
            log.error(f"Executor: Order Error: {e}")
            self._push_failure(symbol, str(e))
            return {"status": "error", "message": str(e)}

    def _push_failure(self, symbol, reason):
        """Push a trade_failed notification so Messenger can surface the error."""
        try:
            self.db.rpush("notifications", json.dumps({
                "type": "trade_failed",
                "data": {"symbol": symbol, "reason": reason},
            }))
        except Exception as e:
            log.warning(f"Executor: Failed to push trade_failed notification for {symbol}: {e}")

    def _place_paper_order(self, symbol, stop_loss_pct=None, take_profit_pct=None, strategy_name=None, signal_id=None):
        """Write order to DB only; no exchange, no Redis active_trades.
        Position size is derived from balance, POSITION_RISK_PCT, and stop loss (no external amount)."""
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                current_bal = shared_db.get_balance(conn, "USDT")
                if current_bal <= 0:
                    msg = f"Non-positive balance ({current_bal:.2f} USDT)"
                    log.error(f"Executor: Paper order skipped: {msg}")
                    self._push_failure(symbol, msg)
                    return {"status": "error", "message": msg}

                # Position sizing: risk POSITION_RISK_PCT of balance per trade
                risk_amount = current_bal * shared_config.POSITION_RISK_PCT
                # If AI provided stop_loss_pct, prefer it for risk sizing; otherwise derive from shared_config.SL_PERCENT
                effective_stop_loss_pct = stop_loss_pct
                if effective_stop_loss_pct is None or effective_stop_loss_pct <= 0:
                    effective_stop_loss_pct = (1 - shared_config.SL_PERCENT) * 100  # e.g. 2% when SL_PERCENT = 0.98
                if effective_stop_loss_pct <= 0:
                    msg = f"Invalid stop loss percent ({effective_stop_loss_pct}%)"
                    log.error(f"Executor: {msg}, aborting paper order")
                    self._push_failure(symbol, msg)
                    return {"status": "error", "message": msg}

                # Liquidation safety: for 3x leverage, liquidation ~33% drop; skip if SL would trigger liquidation first
                if effective_stop_loss_pct >= shared_config.LIQUIDATION_THRESHOLD_PCT:
                    msg = (f"SL {effective_stop_loss_pct:.1f}% >= liquidation threshold "
                           f"{shared_config.LIQUIDATION_THRESHOLD_PCT}% at {shared_config.LEVERAGE}x leverage")
                    log.warning(f"Executor: Order skipped: {msg}")
                    self._push_failure(symbol, msg)
                    return {"status": "error", "message": msg}

                # Position notional in USDT from risk amount and stop loss distance
                position_size_usdt = risk_amount / (effective_stop_loss_pct / 100.0)
                max_notional = current_bal * shared_config.LEVERAGE
                position_size_usdt = min(position_size_usdt, max_notional)
                if position_size_usdt <= 0:
                    msg = "Zero position size after risk sizing"
                    log.error(f"Executor: Paper order skipped: {msg}")
                    self._push_failure(symbol, msg)
                    return {"status": "error", "message": msg}

                # Public ticker for entry/tp/sl (no account interaction)
                ticker = self.exchange.fetch_ticker(symbol)
                base_price = float(ticker['last'])
                # Apply slippage to simulate market order execution delay
                entry_price = self.get_precision_price(symbol, base_price * (1 + shared_config.ENTRY_SLIPPAGE))
                # Same TP/SL logic as live but from slipped entry price
                if effective_stop_loss_pct is not None and effective_stop_loss_pct > 0:
                    sl_price = self.get_precision_price(symbol, entry_price * (1 - effective_stop_loss_pct / 100.0))
                else:
                    sl_price = self.get_precision_price(symbol, entry_price * shared_config.SL_PERCENT)
                if take_profit_pct is not None and take_profit_pct > 0:
                    tp_price = self.get_precision_price(symbol, entry_price * (1 + take_profit_pct / 100.0))
                else:
                    tp_price = self.get_precision_price(symbol, entry_price * shared_config.TP_PERCENT)

                # Quantity from notional and entry price
                raw_qty = position_size_usdt / entry_price
                qty = self.get_precision_amount(symbol, raw_qty)
                final_notional_usdt = float(qty) * entry_price

                # Margin required with leverage and entry fee (taker fee on notional)
                margin_usdt = final_notional_usdt / shared_config.LEVERAGE
                borrowed_amount = max(0.0, final_notional_usdt - margin_usdt)
                entry_fee_usd = final_notional_usdt * shared_config.BINANCE_TAKER_FEE
                total_entry_cost = margin_usdt + entry_fee_usd
                if current_bal < total_entry_cost:
                    msg = (f"Insufficient balance for margin+fee "
                           f"({current_bal:.2f} < {total_entry_cost:.2f} USDT)")
                    log.error(f"Executor: Paper order skipped: {msg}")
                    self._push_failure(symbol, msg)
                    return {"status": "error", "message": msg}

                # Lock margin and pay entry fee from virtual balance
                shared_db.set_balance(conn, "USDT", current_bal - total_entry_cost)
                session = get_trading_session()
                order_id = shared_db.insert_order(
                    conn,
                    symbol=symbol,
                    side="buy",
                    amount_usdt=final_notional_usdt,
                    entry_price=entry_price,
                    quantity=float(qty),
                    tp_price=float(tp_price),
                    sl_price=float(sl_price),
                    entry_fee_usd=float(entry_fee_usd),
                    exchange_order_id=None,
                    borrowed_amount=float(borrowed_amount),
                    hourly_interest_rate=float(shared_config.HOURLY_MARGIN_INTEREST_RATE),
                    strategy_name=strategy_name,
                    session=session,
                    signal_id=signal_id,
                    balance_at_entry=float(current_bal),
                )
            log.info(f"Executor: Risking ${risk_amount:.2f} to buy ${final_notional_usdt:.2f} worth of {symbol} (Leverage: {shared_config.LEVERAGE}x)")
            result = {
                "status": "success",
                "order_id": f"paper-{order_id}",
                "symbol": symbol,
                "entry": entry_price,
                "qty": float(qty),
                "tp": float(tp_price),
                "sl": float(sl_price),
                "timestamp": time.time()
            }
            self.db.rpush('notifications', json.dumps({"type": "trade_confirmed", "data": result}))
            log.info(f"Executor: Paper order written to DB (id={order_id}, {shared_config.LEVERAGE}x leverage) | Session: {session}")
            return result
        except Exception as e:
            log.error(f"Executor: Paper order error: {e}")
            self._push_failure(symbol, str(e))
            return {"status": "error", "message": str(e)}

    def run(self):
        log.info("Executor: Waiting for trade commands from Redis...")
        while True:
            command_data = self.db.blpop('trade_commands', timeout=10)
            if command_data:
                _, payload = command_data
                try:
                    data = json.loads(payload)
                    self.place_smart_order(
                        data.get('symbol'),
                        stop_loss_pct=data.get("stop_loss_pct"),
                        take_profit_pct=data.get("take_profit_pct"),
                        strategy_name=data.get("strategy_name"),
                        signal_id=data.get("signal_id"),
                    )
                except Exception as e:
                    log.error(f"Executor: Parsing error: {e}")


if __name__ == "__main__":
    executor = Executor()
    executor.run()
