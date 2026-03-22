"""Dashboard service — read-only web UI for monitoring algotrader.

Password-protected via HTTP Basic Auth (DASHBOARD_USER / DASHBOARD_PASSWORD env vars).
"""

import base64
import json
import math
import os
import statistics
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import redis as redis_lib
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

sys.path.insert(0, "/app")
from shared import backtest_db, db, config  # noqa: E402
from shared.version import BOT_VERSION  # noqa: E402

STATIC_DIR = Path(__file__).parent / "static"
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "changeme")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


# ── Auth ───────────────────────────────────────────────────────────────────────

def _check_auth(request: Request) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        user, password = decoded.split(":", 1)
        return user == DASHBOARD_USER and password == DASHBOARD_PASSWORD
    except Exception:
        return False


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not _check_auth(request):
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="AlgoTrader Dashboard"'},
            content="Unauthorized",
        )
    return await call_next(request)


# ── Redis ──────────────────────────────────────────────────────────────────────

def _redis():
    try:
        r = redis_lib.Redis(
            host=os.getenv("REDIS_HOST", "localhost"), port=6379, decode_responses=True
        )
        r.ping()
        return r
    except Exception:
        return None


# ── Metric helpers ─────────────────────────────────────────────────────────────

def _profit_factor(rows):
    wins = sum(float(r.get("pnl_usdt") or 0) for r in rows if float(r.get("pnl_usdt") or 0) > 0)
    losses = sum(abs(float(r.get("pnl_usdt") or 0)) for r in rows if float(r.get("pnl_usdt") or 0) < 0)
    return round(wins / losses, 2) if losses > 0 else None


def _max_streaks(rows):
    max_w = cur_w = max_l = cur_l = 0
    for r in rows:
        if float(r.get("pnl_usdt") or 0) > 0:
            cur_w += 1
            cur_l = 0
            if cur_w > max_w:
                max_w = cur_w
        else:
            cur_l += 1
            cur_w = 0
            if cur_l > max_l:
                max_l = cur_l
    return max_w, max_l


def _pnl_buckets(rows):
    buckets = {"lt-5": 0, "-5to-2": 0, "-2to0": 0, "0to2": 0, "2to5": 0, "gt5": 0}
    for r in rows:
        p = float(r.get("pnl_percent") or 0)
        if p < -5:
            buckets["lt-5"] += 1
        elif p < -2:
            buckets["-5to-2"] += 1
        elif p < 0:
            buckets["-2to0"] += 1
        elif p < 2:
            buckets["0to2"] += 1
        elif p < 5:
            buckets["2to5"] += 1
        else:
            buckets["gt5"] += 1
    return buckets


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=(STATIC_DIR / "index.html").read_text())


@app.get("/api/overview")
async def api_overview():
    try:
        with db.get_connection() as conn:
            db.init_schema(conn)
            balance = db.get_balance(conn, "USDT")
            today_pnl = db.get_today_closed_pnl(conn)
            win_rate_data = db.get_recent_signal_win_rate(conn, 20)
            open_orders = db.get_open_orders(conn)
            max_open = db.get_max_open_orders()

            all_closed = [
                dict(r)
                for r in conn.execute(
                    "SELECT pnl_usdt, pnl_percent, hours_held FROM orders "
                    "WHERE status='closed' ORDER BY closed_at ASC"
                ).fetchall()
            ]
            total = len(all_closed)
            best = max((float(r["pnl_usdt"] or 0) for r in all_closed), default=None)
            worst = min((float(r["pnl_usdt"] or 0) for r in all_closed), default=None)
            holds = [float(r["hours_held"]) for r in all_closed if r["hours_held"] is not None]
            avg_hold = round(statistics.mean(holds), 1) if holds else None
            pf = _profit_factor(all_closed)

            daily = sorted(db.get_daily_pnl_history(conn, 30), key=lambda x: x["date"])
            cum = 0.0
            equity = []
            for d in daily:
                cum += float(d["pnl_usdt"])
                equity.append({
                    "date": d["date"],
                    "pnl": round(float(d["pnl_usdt"]), 2),
                    "cumulative": round(cum, 2),
                    "trade_count": d["trade_count"],
                })

        return {
            "balance": round(balance, 2),
            "today_pnl": round(today_pnl, 2),
            "win_rate": win_rate_data,
            "open_orders": len(open_orders),
            "max_open_orders": max_open,
            "total_trades": total,
            "profit_factor": pf,
            "equity_curve": equity,
            "best_trade": round(best, 2) if best is not None else None,
            "worst_trade": round(worst, 2) if worst is not None else None,
            "avg_hold_hours": avg_hold,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/positions")
async def api_positions():
    try:
        with db.get_connection() as conn:
            db.init_schema(conn)
            rows = conn.execute("""
                SELECT id, symbol, side, amount_usdt, entry_price, quantity,
                       tp_price, sl_price, opened_at, mfe_pct, mae_pct,
                       strategy_name, session
                FROM orders WHERE status='open' ORDER BY opened_at DESC
            """).fetchall()
            now = datetime.utcnow()
            out = []
            for r in rows:
                r = dict(r)
                hours_open = None
                if r["opened_at"]:
                    try:
                        dt = datetime.fromisoformat(r["opened_at"].replace("Z", ""))
                        hours_open = round((now - dt).total_seconds() / 3600, 1)
                    except Exception:
                        pass
                entry = float(r["entry_price"] or 0)
                tp = float(r["tp_price"]) if r["tp_price"] else None
                sl = float(r["sl_price"]) if r["sl_price"] else None
                out.append({
                    "id": r["id"],
                    "symbol": r["symbol"],
                    "side": r["side"],
                    "entry_price": entry,
                    "tp_price": tp,
                    "sl_price": sl,
                    "tp_dist_pct": round((tp - entry) / entry * 100, 2) if tp and entry else None,
                    "sl_dist_pct": round((entry - sl) / entry * 100, 2) if sl and entry else None,
                    "amount_usdt": float(r["amount_usdt"] or 0),
                    "hours_open": hours_open,
                    "mfe_pct": float(r["mfe_pct"]) if r["mfe_pct"] is not None else None,
                    "mae_pct": float(r["mae_pct"]) if r["mae_pct"] is not None else None,
                    "strategy": r["strategy_name"],
                    "session": r["session"],
                    "opened_at": r["opened_at"],
                })
        return out
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/trades")
async def api_trades(period: str = "week"):
    if period not in {"today", "week", "month", "all"}:
        period = "week"
    try:
        with db.get_connection() as conn:
            db.init_schema(conn)
            where = db._closed_orders_where(period)
            rows = conn.execute(f"""
                SELECT id, symbol, side, entry_price, exit_price, pnl_usdt, pnl_percent,
                       net_pnl_pct, close_reason, strategy_name, session, hours_held,
                       mfe_pct, mae_pct, opened_at, closed_at, amount_usdt
                FROM orders WHERE {where} ORDER BY closed_at DESC
            """).fetchall()
            trades = []
            for r in rows:
                t = dict(r)
                for k in ("pnl_usdt", "pnl_percent", "net_pnl_pct"):
                    if t[k] is None:
                        t[k] = 0.0
                trades.append(t)
            net_pnl = sum(t["pnl_usdt"] for t in trades)
            wins = sum(1 for t in trades if t["pnl_usdt"] > 0)
        return {
            "trades": trades,
            "summary": {
                "total": len(trades),
                "wins": wins,
                "losses": len(trades) - wins,
                "net_pnl": round(net_pnl, 2),
                "avg_pnl": round(net_pnl / len(trades), 2) if trades else 0.0,
            },
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/analytics")
async def api_analytics():
    try:
        with db.get_connection() as conn:
            db.init_schema(conn)
            rows = [
                dict(r)
                for r in conn.execute("""
                    SELECT pnl_usdt, pnl_percent, strategy_name, session, close_reason,
                           hours_held, mfe_pct, mae_pct, closed_at
                    FROM orders WHERE status='closed' ORDER BY closed_at ASC
                """).fetchall()
            ]

            by_strategy = {}
            for r in rows:
                s = r["strategy_name"] or "UNKNOWN"
                if s not in by_strategy:
                    by_strategy[s] = {"pnl": 0.0, "wins": 0, "total": 0}
                by_strategy[s]["pnl"] += float(r["pnl_usdt"] or 0)
                by_strategy[s]["total"] += 1
                if float(r["pnl_usdt"] or 0) > 0:
                    by_strategy[s]["wins"] += 1
            for s, st in by_strategy.items():
                st["pnl"] = round(st["pnl"], 2)
                st["win_rate"] = round(st["wins"] / st["total"] * 100, 1) if st["total"] else 0

            by_session = {}
            for r in rows:
                s = r["session"] or "UNKNOWN"
                if s not in by_session:
                    by_session[s] = {"pnl": 0.0, "wins": 0, "total": 0}
                by_session[s]["pnl"] += float(r["pnl_usdt"] or 0)
                by_session[s]["total"] += 1
                if float(r["pnl_usdt"] or 0) > 0:
                    by_session[s]["wins"] += 1
            for s, st in by_session.items():
                st["pnl"] = round(st["pnl"], 2)
                st["win_rate"] = round(st["wins"] / st["total"] * 100, 1) if st["total"] else 0

            by_reason = {}
            for r in rows:
                rs = r["close_reason"] or "UNKNOWN"
                by_reason[rs] = by_reason.get(rs, 0) + 1

            total = len(rows)
            wins_list = [r for r in rows if float(r["pnl_usdt"] or 0) > 0]
            losses_list = [r for r in rows if float(r["pnl_usdt"] or 0) <= 0]
            win_rate = round(len(wins_list) / total * 100, 1) if total else 0
            avg_win = round(statistics.mean(float(r["pnl_usdt"] or 0) for r in wins_list), 2) if wins_list else 0
            avg_loss = round(statistics.mean(float(r["pnl_usdt"] or 0) for r in losses_list), 2) if losses_list else 0
            pf = _profit_factor(rows)
            max_w, max_l = _max_streaks(rows)
            buckets = _pnl_buckets(rows)

            scatter = [
                {"mfe": float(r["mfe_pct"]), "mae": float(r["mae_pct"]), "pnl": float(r["pnl_usdt"] or 0)}
                for r in rows
                if r["mfe_pct"] is not None and r["mae_pct"] is not None
            ][:200]

            holds = [float(r["hours_held"]) for r in rows if r["hours_held"] is not None]
            mfes = [float(r["mfe_pct"]) for r in rows if r["mfe_pct"] is not None]
            maes = [float(r["mae_pct"]) for r in rows if r["mae_pct"] is not None]
            avg_hold = round(statistics.mean(holds), 1) if holds else None
            avg_mfe = round(statistics.mean(mfes), 2) if mfes else None
            avg_mae = round(statistics.mean(maes), 2) if maes else None

            daily = db.get_daily_pnl_history(conn, 90)
            sharpe = None
            max_drawdown = None
            if len(daily) >= 2:
                pnls = [float(r["pnl_usdt"]) for r in daily]
                mean_p = statistics.mean(pnls)
                std_p = statistics.stdev(pnls)
                sharpe = round(mean_p / std_p * math.sqrt(365), 2) if std_p > 0 else None
                sorted_daily = sorted(daily, key=lambda x: x["date"])
                cum = peak = dd = 0.0
                for r in sorted_daily:
                    cum += float(r["pnl_usdt"])
                    if cum > peak:
                        peak = cum
                    dd = max(dd, peak - cum)
                max_drawdown = round(dd, 2)

            sig_rows = conn.execute(
                "SELECT verdict, COUNT(*) as cnt FROM signals WHERE verdict IS NOT NULL GROUP BY verdict"
            ).fetchall()
            signals_by_verdict = {r["verdict"]: r["cnt"] for r in sig_rows}

            cal_rows = db.get_daily_pnl_history(conn, 30)
            calendar = {
                r["date"]: {"pnl": round(float(r["pnl_usdt"]), 2), "count": r["trade_count"]}
                for r in cal_rows
            }

        return {
            "total": total,
            "win_count": len(wins_list),
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": pf,
            "max_win_streak": max_w,
            "max_loss_streak": max_l,
            "avg_hold_hours": avg_hold,
            "avg_mfe": avg_mfe,
            "avg_mae": avg_mae,
            "sharpe": sharpe,
            "max_drawdown": max_drawdown,
            "by_strategy": by_strategy,
            "by_session": by_session,
            "by_close_reason": by_reason,
            "pnl_distribution": buckets,
            "scatter": scatter,
            "signals_by_verdict": signals_by_verdict,
            "calendar": calendar,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/market")
async def api_market():
    try:
        r = _redis()
        btc = active = candidates = None
        if r:
            raw = r.get(config.REDIS_KEY_BTC_CONTEXT)
            if raw:
                try:
                    btc = json.loads(raw)
                except Exception:
                    pass
            raw2 = r.get(config.REDIS_KEY_ACTIVE_SYMBOLS)
            if raw2:
                try:
                    active = json.loads(raw2)
                except Exception:
                    pass
            raw3 = r.get("filtered_candidates")
            if raw3:
                try:
                    cands = json.loads(raw3)
                    candidates = [
                        {k: c.get(k) for k in ("symbol", "last_price", "change_24h", "rsi", "rvol", "filter_score")}
                        for c in cands
                    ]
                except Exception:
                    pass
            regime = None
            raw4 = r.get(config.REDIS_KEY_MARKET_REGIME)
            if raw4:
                try:
                    regime = json.loads(raw4)
                except Exception:
                    pass
        return {
            "btc_context": btc,
            "active_symbols": active or [],
            "filtered_candidates": candidates or [],
            "market_regime": regime,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/settings")
async def api_settings():
    try:
        with db.get_connection() as conn:
            db.init_schema(conn)
            s = {
                "strategy": db.get_setting(conn, config.SYSTEM_KEY_STRATEGY, "CONSERVATIVE"),
                "autopilot": db.get_setting(conn, config.SYSTEM_KEY_AUTOPILOT, "0"),
                "trading_paused": db.get_setting(conn, config.SYSTEM_KEY_TRADING_PAUSED, "0"),
                "max_open_orders": db.get_setting(conn, config.SYSTEM_KEY_MAX_OPEN_ORDERS, str(config.MAX_OPEN_ORDERS_DEFAULT)),
                "max_symbols": db.get_setting(conn, config.SYSTEM_KEY_MAX_SYMBOLS, str(config.MAX_SYMBOLS_DEFAULT)),
            }
        return {
            **s,
            "leverage": config.LEVERAGE,
            "position_risk_pct": round(config.POSITION_RISK_PCT * 100, 1),
            "risk_guard_max_sl": config.RISK_GUARD_MAX_SL,
            "risk_guard_min_rr": config.RISK_GUARD_MIN_RR,
            "taker_fee_pct": round(config.BINANCE_TAKER_FEE * 100, 2),
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── Backtest routes ────────────────────────────────────────────────────────────

@app.post("/api/backtest/start")
async def api_backtest_start(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    strategy = str(body.get("strategy", "CONSERVATIVE")).upper()
    if strategy not in {"CONSERVATIVE", "AGGRESSIVE", "REVERSAL"}:
        strategy = "CONSERVATIVE"
    initial_balance = float(body.get("balance", 1000.0))

    # Block if a run is already in progress
    with backtest_db.get_connection() as conn:
        backtest_db.init_schema(conn)
        active = backtest_db.get_active_run(conn)
        if active:
            return JSONResponse(
                status_code=409,
                content={"error": "A backtest run is already in progress", "run_id": active["id"]},
            )

    run_id = str(uuid.uuid4())
    yesterday = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    start_date = (yesterday - timedelta(days=365)).strftime("%Y-%m-%d")
    end_date = yesterday.strftime("%Y-%m-%d")

    with backtest_db.get_connection() as conn:
        backtest_db.init_schema(conn)
        backtest_db.create_run(
            conn, run_id=run_id, bot_version=BOT_VERSION, strategy=strategy,
            start_date=start_date, end_date=end_date, initial_balance=initial_balance,
        )

    # Spawn backtest subprocess — non-blocking
    script_path = "/app/scripts/backtest.py"
    if not os.path.exists(script_path):
        script_path = str(Path(__file__).parent.parent.parent / "scripts" / "backtest.py")
    subprocess.Popen(
        [sys.executable, script_path,
         "--run-id", run_id,
         "--strategy", strategy,
         "--balance", str(initial_balance)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return {"run_id": run_id, "strategy": strategy, "balance": initial_balance,
            "start_date": start_date, "end_date": end_date}


@app.get("/api/backtest/status")
async def api_backtest_status():
    try:
        with backtest_db.get_connection() as conn:
            backtest_db.init_schema(conn)
            active = backtest_db.get_active_run(conn)
            return active or {"status": "idle"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/backtest/results")
async def api_backtest_results():
    try:
        with backtest_db.get_connection() as conn:
            backtest_db.init_schema(conn)
            runs = backtest_db.get_all_runs(conn)
            return runs
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/backtest/trades/{run_id}")
async def api_backtest_trades(run_id: str):
    try:
        with backtest_db.get_connection() as conn:
            backtest_db.init_schema(conn)
            trades = backtest_db.get_trades_for_run(conn, run_id)
            return trades
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
