"""
SQLite persistence for orders (trades), balance, system settings, and AI signals.
Used by Executor, Monitor, Messenger, Filter, Brain, Scout.
"""
import os
import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, List, Optional

# Default path: ./data/algotrader.db (or /data/algotrader.db in Docker with volume)
DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "algotrader.db")


def get_database_path() -> str:
    return os.getenv("DATABASE_PATH", DEFAULT_DB_PATH)


def _add_orders_columns_if_missing(conn: sqlite3.Connection) -> None:
    """Add new order columns for existing DBs that were created before these columns existed."""
    cur = conn.execute("PRAGMA table_info(orders)")
    existing = {row[1] for row in cur.fetchall()}
    for col, spec in [
        ("entry_fee_usd", "REAL NOT NULL DEFAULT 0"),
        ("exit_fee_usd", "REAL NOT NULL DEFAULT 0"),
        ("margin_interest_paid", "REAL NOT NULL DEFAULT 0"),
        ("net_pnl_pct", "REAL"),
        ("borrowed_amount", "REAL NOT NULL DEFAULT 0"),
        ("hourly_interest_rate", "REAL"),
        ("strategy_name", "TEXT"),
        ("session", "TEXT"),
        ("signal_id", "TEXT"),
        ("exit_price", "REAL"),
        ("hours_held", "REAL"),
        ("mfe_pct", "REAL"),
        ("mae_pct", "REAL"),
        ("balance_at_entry", "REAL"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {spec}")
            existing.add(col)


def _add_signals_columns_if_missing(conn: sqlite3.Connection) -> None:
    """Add new signal columns for existing DBs that were created before these columns existed."""
    cur = conn.execute("PRAGMA table_info(signals)")
    existing = {row[1] for row in cur.fetchall()}
    for col, spec in [
        ("verdict", "TEXT"),
        ("outcome", "TEXT"),
        ("outcome_pnl_usdt", "REAL"),
        ("outcome_pnl_pct", "REAL"),
        ("outcome_close_reason", "TEXT"),
        ("outcome_closed_at", "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {spec}")


@contextmanager
def get_connection():
    path = get_database_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL DEFAULT 'buy',
            amount_usdt REAL NOT NULL,
            entry_price REAL NOT NULL,
            quantity REAL NOT NULL,
            tp_price REAL,
            sl_price REAL,
            status TEXT NOT NULL DEFAULT 'open',
            exchange_order_id TEXT,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            pnl_usdt REAL,
            pnl_percent REAL,
            close_reason TEXT,
            entry_fee_usd REAL NOT NULL DEFAULT 0,
            exit_fee_usd REAL NOT NULL DEFAULT 0,
            margin_interest_paid REAL NOT NULL DEFAULT 0,
            net_pnl_pct REAL,
            borrowed_amount REAL NOT NULL DEFAULT 0,
            hourly_interest_rate REAL,
            strategy_name TEXT,
            session TEXT,
            signal_id TEXT,
            exit_price REAL,
            hours_held REAL,
            mfe_pct REAL,
            mae_pct REAL,
            balance_at_entry REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_orders_symbol_status ON orders(symbol, status);
        CREATE INDEX IF NOT EXISTS idx_orders_opened_at ON orders(opened_at);
        CREATE INDEX IF NOT EXISTS idx_orders_closed_at ON orders(closed_at);
        CREATE INDEX IF NOT EXISTS idx_orders_signal_id ON orders(signal_id);
    """)
    _add_orders_columns_if_missing(conn)

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS balance (
            currency TEXT PRIMARY KEY,
            amount REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS signals (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            verdict TEXT,
            stats_json TEXT NOT NULL,
            prompt TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS daily_pnl (
            date TEXT PRIMARY KEY,
            pnl_usdt REAL NOT NULL DEFAULT 0,
            trade_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    _add_signals_columns_if_missing(conn)


def get_setting(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    """Return value for key from settings table, or default if missing."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Insert or update a setting."""
    now = datetime.utcnow().isoformat() + "Z"
    conn.execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, value, now),
    )


def get_setting_value(key: str, default: Optional[str] = None) -> Optional[str]:
    """Convenience: open connection, return setting value, close. For services that read one key."""
    with get_connection() as conn:
        init_schema(conn)
        return get_setting(conn, key, default)


def insert_order(
    conn: sqlite3.Connection,
    symbol: str,
    side: str,
    amount_usdt: float,
    entry_price: float,
    quantity: float,
    tp_price: float,
    sl_price: float,
    entry_fee_usd: float = 0.0,
    exchange_order_id: Optional[str] = None,
    borrowed_amount: float = 0.0,
    hourly_interest_rate: Optional[float] = None,
    strategy_name: Optional[str] = None,
    session: Optional[str] = None,
    signal_id: Optional[str] = None,
    balance_at_entry: Optional[float] = None,
) -> int:
    now = datetime.utcnow().isoformat() + "Z"
    cur = conn.execute(
        """INSERT INTO orders (symbol, side, amount_usdt, entry_price, quantity, tp_price, sl_price,
                               status, exchange_order_id, opened_at, entry_fee_usd, borrowed_amount,
                               hourly_interest_rate, strategy_name, session, signal_id, balance_at_entry)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (symbol, side, amount_usdt, entry_price, quantity, tp_price, sl_price, exchange_order_id,
         now, entry_fee_usd, borrowed_amount, hourly_interest_rate, strategy_name, session,
         signal_id, balance_at_entry),
    )
    return cur.lastrowid


def insert_signal(
    conn: sqlite3.Connection,
    signal_id: str,
    symbol: str,
    stats: dict,
    prompt: str,
    response: dict,
) -> None:
    """Insert one AI signal row (stats we sent to AI, prompt, parsed response)."""
    now = datetime.utcnow().isoformat() + "Z"
    verdict = None
    try:
        verdict = str(response.get("verdict", "")).upper()
    except Exception:
        verdict = None
    conn.execute(
        """INSERT OR REPLACE INTO signals (id, symbol, verdict, stats_json, prompt, response_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (signal_id, symbol, verdict, json.dumps(stats), prompt, json.dumps(response), now),
    )


def get_open_orders(conn: sqlite3.Connection) -> List[Any]:
    """Return all open orders (status='open'), newest first."""
    cur = conn.execute(
        """SELECT id, symbol, side, amount_usdt, entry_price, quantity, tp_price, sl_price,
                  exchange_order_id, opened_at, mfe_pct, mae_pct
           FROM orders WHERE status = 'open' ORDER BY opened_at DESC"""
    )
    return [dict(row) for row in cur.fetchall()]


def get_open_order_id_for_symbol(conn: sqlite3.Connection, symbol: str) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM orders WHERE symbol = ? AND status = 'open' ORDER BY opened_at DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    return row["id"] if row else None


def get_open_order_for_symbol(conn: sqlite3.Connection, symbol: str) -> Optional[Any]:
    """Return one open order row for symbol (id, symbol, entry_price, quantity, tp_price, sl_price, etc.) or None."""
    row = conn.execute(
        """SELECT id, symbol, entry_price, quantity, tp_price, sl_price, amount_usdt, opened_at, entry_fee_usd, borrowed_amount, hourly_interest_rate, strategy_name, signal_id
           FROM orders WHERE symbol = ? AND status = 'open' ORDER BY opened_at DESC LIMIT 1""",
        (symbol,),
    ).fetchone()
    return dict(row) if row else None


def update_order_closed(
    conn: sqlite3.Connection,
    order_id: int,
    pnl_usdt: float,
    pnl_percent: float,
    close_reason: str,
    exit_fee_usd: float,
    margin_interest_paid: float,
    net_pnl_pct: float,
    exit_price: Optional[float] = None,
    hours_held: Optional[float] = None,
    mfe_pct: Optional[float] = None,
    mae_pct: Optional[float] = None,
) -> None:
    now = datetime.utcnow().isoformat() + "Z"
    conn.execute(
        """UPDATE orders
           SET status = 'closed',
               closed_at = ?,
               pnl_usdt = ?,
               pnl_percent = ?,
               close_reason = ?,
               exit_fee_usd = ?,
               margin_interest_paid = ?,
               net_pnl_pct = ?,
               exit_price = ?,
               hours_held = ?,
               mfe_pct = ?,
               mae_pct = ?
           WHERE id = ?""",
        (now, pnl_usdt, pnl_percent, close_reason, exit_fee_usd, margin_interest_paid,
         net_pnl_pct, exit_price, hours_held, mfe_pct, mae_pct, order_id),
    )


def update_order_extremes(conn: sqlite3.Connection, order_id: int, mfe_pct: float, mae_pct: float) -> None:
    """Update MFE/MAE for an open order. Called by Monitor on each price tick when extremes change."""
    conn.execute(
        "UPDATE orders SET mfe_pct = ?, mae_pct = ? WHERE id = ? AND status = 'open'",
        (mfe_pct, mae_pct, order_id),
    )


def record_daily_pnl(conn: sqlite3.Connection, pnl_usdt_delta: float) -> None:
    """Accumulate closed-trade PnL into today's daily_pnl row (UTC). Called by Monitor on close."""
    today = datetime.utcnow().date().isoformat()
    now = datetime.utcnow().isoformat() + "Z"
    conn.execute(
        """INSERT INTO daily_pnl (date, pnl_usdt, trade_count, updated_at)
           VALUES (?, ?, 1, ?)
           ON CONFLICT(date) DO UPDATE SET
               pnl_usdt = pnl_usdt + excluded.pnl_usdt,
               trade_count = trade_count + 1,
               updated_at = excluded.updated_at""",
        (today, pnl_usdt_delta, now),
    )


def get_daily_pnl_history(conn: sqlite3.Connection, days: int = 30) -> List[dict]:
    """Return daily PnL rows for the last N days, newest first."""
    cur = conn.execute(
        "SELECT date, pnl_usdt, trade_count FROM daily_pnl ORDER BY date DESC LIMIT ?",
        (days,),
    )
    return [dict(row) for row in cur.fetchall()]


def update_signal_outcome(
    conn: sqlite3.Connection,
    signal_id: str,
    outcome: str,
    pnl_usdt: float,
    pnl_pct: float,
    close_reason: str,
) -> None:
    """Record the trade result (WIN/LOSS/BREAKEVEN) back to the originating AI signal row."""
    now = datetime.utcnow().isoformat() + "Z"
    conn.execute(
        """UPDATE signals
           SET outcome = ?, outcome_pnl_usdt = ?, outcome_pnl_pct = ?,
               outcome_close_reason = ?, outcome_closed_at = ?
           WHERE id = ?""",
        (outcome, pnl_usdt, pnl_pct, close_reason, now, signal_id),
    )


def get_recent_signal_win_rate(conn: sqlite3.Connection, limit: int = 20) -> dict:
    """Win rate stats for the last N resolved BUY signals.

    Returns dict: total, wins, win_rate_pct (None when no data), avg_pnl_usdt.
    Only counts signals that have an outcome recorded (i.e. the trade closed).
    """
    cur = conn.execute(
        """SELECT outcome, outcome_pnl_usdt
           FROM signals
           WHERE outcome IS NOT NULL AND verdict = 'BUY'
           ORDER BY outcome_closed_at DESC
           LIMIT ?""",
        (limit,),
    )
    rows = cur.fetchall()
    if not rows:
        return {"total": 0, "wins": 0, "win_rate_pct": None, "avg_pnl_usdt": None}
    total = len(rows)
    wins = sum(1 for r in rows if r["outcome"] == "WIN")
    total_pnl = sum(r["outcome_pnl_usdt"] or 0.0 for r in rows)
    return {
        "total": total,
        "wins": wins,
        "win_rate_pct": round(wins / total * 100, 1),
        "avg_pnl_usdt": round(total_pnl / total, 2),
    }


def update_order_sl_price(conn: sqlite3.Connection, order_id: int, new_sl_price: float) -> None:
    """Move the stop-loss price upward for a trailing stop (only updates open orders)."""
    conn.execute(
        "UPDATE orders SET sl_price = ? WHERE id = ? AND status = 'open'",
        (new_sl_price, order_id),
    )


def get_balance(conn: sqlite3.Connection, currency: str) -> float:
    row = conn.execute("SELECT amount FROM balance WHERE currency = ?", (currency,)).fetchone()
    return float(row["amount"]) if row else 0.0


def get_today_closed_pnl(conn: sqlite3.Connection) -> float:
    """Sum pnl_usdt for orders closed today (UTC). Returns 0.0 if none."""
    row = conn.execute(
        """SELECT COALESCE(SUM(pnl_usdt), 0) AS total
           FROM orders WHERE status = 'closed' AND date(closed_at) = date('now')"""
    ).fetchone()
    return float(row["total"]) if row else 0.0


def _closed_orders_where(period: str) -> str:
    """Return SQL WHERE fragment for closed orders by period."""
    if period == "today":
        return "status = 'closed' AND date(closed_at) = date('now')"
    if period == "yesterday" or period == "last":
        return "status = 'closed' AND date(closed_at) = date('now', '-1 day')"
    if period == "week":
        return "status = 'closed' AND closed_at >= datetime('now', '-7 days')"
    if period == "month":
        return "status = 'closed' AND closed_at >= datetime('now', '-30 days')"
    return "status = 'closed'"


def get_closed_orders_with_signals(
    conn: sqlite3.Connection,
    period: str,
) -> List[dict]:
    """Fetch closed orders joined with their AI signals by signal_id for analytics.
    period: 'today'|'yesterday'|'last'|'week'|'month'|'all'.
    Returns list of dicts with: symbol, strategy_name, ai_reason, rsi_at_entry, rvol_at_entry,
    entry_price, exit_price (derived), pnl_usdt, pnl_percent, close_reason, opened_at, closed_at,
    mfe_pct (None when intratrade high not stored). Orders without a matching signal still appear
    with ai_reason/rsi/rvol as None."""
    where = _closed_orders_where(period)
    cur = conn.execute(
        f"""
        SELECT
            o.id, o.symbol, o.strategy_name, o.entry_price, o.quantity, o.tp_price, o.sl_price,
            o.opened_at, o.closed_at, o.pnl_usdt, o.pnl_percent, o.close_reason, o.signal_id,
            o.exit_price, o.hours_held, o.mfe_pct, o.mae_pct,
            s.stats_json, s.response_json
        FROM orders o
        LEFT JOIN signals s ON o.signal_id = s.id
        WHERE {where}
        ORDER BY o.closed_at DESC
        """
    )
    rows = []
    for row in cur.fetchall():
        row_dict = dict(row)
        entry = float(row_dict["entry_price"])
        pnl_pct = float(row_dict["pnl_percent"]) if row_dict.get("pnl_percent") is not None else None
        # Use stored exit_price when available; fall back to computing it for old rows
        if row_dict.get("exit_price") is not None:
            exit_price = float(row_dict["exit_price"])
        elif pnl_pct is not None:
            exit_price = entry * (1 + pnl_pct / 100.0)
        else:
            exit_price = None

        rsi_at_entry = None
        rvol_at_entry = None
        change_24h_at_entry = None
        volume_24h_at_entry = None
        atr_at_entry = None
        ema_alignment_15m = None
        ema_alignment_1h = None
        macd_hist_15m = None
        bb_pct_b_15m = None
        btc_bias_at_entry = None
        bot_version_at_signal = None
        ai_reason = None
        ai_confidence = None
        ai_setup_grade = None

        try:
            if row_dict.get("stats_json"):
                stats = json.loads(row_dict["stats_json"])
                rsi_at_entry = stats.get("rsi")
                rvol_at_entry = stats.get("rvol")
                change_24h_at_entry = stats.get("change_24h")
                volume_24h_at_entry = stats.get("volume_24h")
                atr_at_entry = stats.get("atr_at_entry")
                ema_alignment_15m = stats.get("ema_alignment_15m")
                ema_alignment_1h = stats.get("ema_alignment_1h")
                macd_hist_15m = stats.get("macd_hist_15m")
                bb_pct_b_15m = stats.get("bb_pct_b_15m")
                btc_bias_at_entry = stats.get("btc_bias")
                bot_version_at_signal = stats.get("bot_version")
        except (json.JSONDecodeError, TypeError):
            pass
        try:
            if row_dict.get("response_json"):
                resp = json.loads(row_dict["response_json"])
                ai_reason = resp.get("reason")
                ai_confidence = resp.get("confidence")
                ai_setup_grade = resp.get("setup_grade")
        except (json.JSONDecodeError, TypeError):
            pass

        rows.append({
            "symbol": row_dict["symbol"],
            "strategy_name": row_dict.get("strategy_name"),
            "ai_reason": ai_reason,
            "ai_confidence": ai_confidence,
            "ai_setup_grade": ai_setup_grade,
            "rsi_at_entry": rsi_at_entry,
            "rvol_at_entry": rvol_at_entry,
            "change_24h_at_entry": change_24h_at_entry,
            "volume_24h_at_entry": volume_24h_at_entry,
            "atr_at_entry": atr_at_entry,
            "ema_alignment_15m": ema_alignment_15m,
            "ema_alignment_1h": ema_alignment_1h,
            "macd_hist_15m": macd_hist_15m,
            "bb_pct_b_15m": bb_pct_b_15m,
            "btc_bias_at_entry": btc_bias_at_entry,
            "bot_version": bot_version_at_signal,
            "entry_price": entry,
            "exit_price": exit_price,
            "pnl_usdt": float(row_dict["pnl_usdt"]) if row_dict.get("pnl_usdt") is not None else None,
            "pnl_percent": pnl_pct,
            "close_reason": row_dict.get("close_reason"),
            "opened_at": row_dict.get("opened_at"),
            "closed_at": row_dict.get("closed_at"),
            "hours_held": row_dict.get("hours_held"),
            "mfe_pct": row_dict.get("mfe_pct"),
            "mae_pct": row_dict.get("mae_pct"),
        })
    return rows


def get_closed_orders_stats(
    conn: sqlite3.Connection,
    period: str,
) -> dict:
    """Stats for closed orders in the given period. period: 'today'|'yesterday'|'week'|'month'|'all'.
    Returns dict: total_pnl, count, count_sl, count_tp, count_manual, count_successful."""
    where = _closed_orders_where(period)
    row = conn.execute(
        f"""
        SELECT
            COALESCE(SUM(pnl_usdt), 0) AS total_pnl,
            COUNT(*) AS count,
            SUM(CASE WHEN close_reason LIKE 'STOP-LOSS%' THEN 1 ELSE 0 END) AS count_sl,
            SUM(CASE WHEN close_reason LIKE 'TAKE-PROFIT%' THEN 1 ELSE 0 END) AS count_tp,
            SUM(CASE WHEN close_reason LIKE 'Manual%' THEN 1 ELSE 0 END) AS count_manual,
            SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) AS count_successful
        FROM orders
        WHERE {where}
        """
    ).fetchone()
    if not row or row["count"] == 0:
        return {
            "total_pnl": 0.0,
            "count": 0,
            "count_sl": 0,
            "count_tp": 0,
            "count_manual": 0,
            "count_successful": 0,
        }
    return {
        "total_pnl": float(row["total_pnl"]),
        "count": int(row["count"]),
        "count_sl": int(row["count_sl"]),
        "count_tp": int(row["count_tp"]),
        "count_manual": int(row["count_manual"]),
        "count_successful": int(row["count_successful"]),
    }


def set_balance(conn: sqlite3.Connection, currency: str, amount: float) -> None:
    now = datetime.utcnow().isoformat() + "Z"
    conn.execute(
        """INSERT INTO balance (currency, amount, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(currency) DO UPDATE SET amount = excluded.amount, updated_at = excluded.updated_at""",
        (currency, amount, now),
    )


def sync_balance_from_exchange(conn: sqlite3.Connection, exchange) -> None:
    """Fetch USDT balance from exchange and write to DB."""
    try:
        balance = exchange.fetch_balance()
        free = float(balance.get("free", {}).get("USDT", 0))
        set_balance(conn, "USDT", free)
    except Exception:
        pass
