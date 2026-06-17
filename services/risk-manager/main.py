"""Risk-manager service: daily drawdown circuit breaker, portfolio exposure cap, balance guardrail."""

import json
import os
import sys
import time

import redis

sys.path.insert(0, "/app")
import shared.db as shared_db
from shared import config as shared_config
from shared.logger import get_logger

log = get_logger("risk-manager")

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
CHECK_INTERVAL_S = 10

DAILY_DRAWDOWN_LIMIT_PCT = float(os.getenv("RISK_DAILY_DRAWDOWN_PCT", str(shared_config.RISK_DAILY_DRAWDOWN_PCT)))
PORTFOLIO_EXPOSURE_LIMIT_PCT = float(os.getenv("RISK_PORTFOLIO_EXPOSURE_PCT", str(shared_config.RISK_PORTFOLIO_EXPOSURE_PCT)))
MIN_BALANCE_USDT = float(os.getenv("RISK_MIN_BALANCE_USDT", str(shared_config.RISK_MIN_BALANCE_USDT)))


class RiskManager:
    def __init__(self):
        self.db = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        self._paused_by_risk = False

    def _push_alert(self, reason: str, detail: str) -> None:
        try:
            self.db.rpush("notifications", json.dumps({
                "type": "risk_manager_alert",
                "data": {"reason": reason, "detail": detail},
            }))
        except Exception as e:
            log.error(f"RiskManager: failed to push alert: {e}")

    def _pause_trading(self, reason: str, detail: str) -> None:
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                shared_db.set_setting(conn, shared_config.SYSTEM_KEY_TRADING_PAUSED, "1")
            self._paused_by_risk = True
            log.warning(f"RiskManager: PAUSED — {reason}: {detail}")
            self._push_alert(reason, detail)
        except Exception as e:
            log.error(f"RiskManager: failed to pause trading: {e}")

    def check(self) -> None:
        try:
            with shared_db.get_connection() as conn:
                shared_db.init_schema(conn)
                balance = shared_db.get_balance(conn, "USDT")
                paused = shared_db.get_setting(conn, shared_config.SYSTEM_KEY_TRADING_PAUSED, "0")
                today_pnl = shared_db.get_today_closed_pnl(conn)
                open_orders = shared_db.get_open_orders(conn)

            # 1. Balance guardrail — don't trade on a near-empty account
            if 0 < balance < MIN_BALANCE_USDT:
                if paused != "1":
                    self._pause_trading(
                        "LOW BALANCE",
                        f"Balance ${balance:.2f} is below the ${MIN_BALANCE_USDT:.2f} minimum. "
                        "Add funds then send `start` to resume.",
                    )
                return

            # 2. Daily drawdown circuit breaker
            if balance > 0 and today_pnl < 0:
                drawdown_pct = abs(today_pnl) / balance * 100
                if drawdown_pct >= DAILY_DRAWDOWN_LIMIT_PCT:
                    if paused != "1":
                        self._pause_trading(
                            "DAILY DRAWDOWN LIMIT",
                            f"Today's loss ${abs(today_pnl):.2f} ({drawdown_pct:.1f}%) reached the "
                            f"{DAILY_DRAWDOWN_LIMIT_PCT:.0f}% daily limit. "
                            "Trading paused until tomorrow — send `start` to override.",
                        )
                    return

            # 3. Portfolio notional exposure cap
            if open_orders and balance > 0:
                total_notional = sum(float(o.get("amount_usdt", 0)) for o in open_orders)
                exposure_pct = total_notional / balance * 100
                if exposure_pct > PORTFOLIO_EXPOSURE_LIMIT_PCT:
                    if paused != "1":
                        self._pause_trading(
                            "PORTFOLIO EXPOSURE CAP",
                            f"Open notional ${total_notional:.2f} ({exposure_pct:.0f}% of balance) "
                            f"exceeds the {PORTFOLIO_EXPOSURE_LIMIT_PCT:.0f}% cap. "
                            "No new positions until existing ones close.",
                        )
                    return

            if self._paused_by_risk:
                log.info("RiskManager: all checks passing — trading remains paused until user sends `start`")

        except Exception as e:
            log.error(f"RiskManager: check error: {e}")

    def run(self) -> None:
        log.info(
            f"RiskManager: started | "
            f"daily_drawdown={DAILY_DRAWDOWN_LIMIT_PCT}% | "
            f"portfolio_cap={PORTFOLIO_EXPOSURE_LIMIT_PCT}% | "
            f"min_balance=${MIN_BALANCE_USDT}"
        )
        while True:
            self.check()
            time.sleep(CHECK_INTERVAL_S)


if __name__ == "__main__":
    RiskManager().run()
