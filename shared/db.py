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
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {spec}")
            existing.add(col)


def _add_signals_columns_if_missing(conn: sqlite3.Connection) -> None:
    """Add new signal columns for existing DBs that were created before these columns existed."""
    cur = conn.execute("PRAGMA table_info(signals)")
    existing = {row[1] for row in cur.fetchall()}
    if "verdict" not in existing:
        conn.execute("ALTER TABLE signals ADD COLUMN verdict TEXT")


@contextmanager
def get_connection():
    path = get_database_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
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
) -> int:
    now = datetime.utcnow().isoformat() + "Z"
    cur = conn.execute(
        """INSERT INTO orders (symbol, side, amount_usdt, entry_price, quantity, tp_price, sl_price, status, exchange_order_id, opened_at, entry_fee_usd, borrowed_amount, hourly_interest_rate, strategy_name, session, signal_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (symbol, side, amount_usdt, entry_price, quantity, tp_price, sl_price, exchange_order_id, now, entry_fee_usd, borrowed_amount, hourly_interest_rate, strategy_name, session, signal_id),
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
    """Return all open orders (status='open'), newest first. Rows as dict-like with keys: symbol, entry_price, quantity, tp_price, sl_price, amount_usdt, opened_at, exchange_order_id."""
    cur = conn.execute(
        """SELECT id, symbol, side, amount_usdt, entry_price, quantity, tp_price, sl_price, exchange_order_id, opened_at
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
        """SELECT id, symbol, entry_price, quantity, tp_price, sl_price, amount_usdt, opened_at, entry_fee_usd, borrowed_amount, hourly_interest_rate, strategy_name
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
               net_pnl_pct = ?
           WHERE id = ?""",
        (now, pnl_usdt, pnl_percent, close_reason, exit_fee_usd, margin_interest_paid, net_pnl_pct, order_id),
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
        pnl_pct = row_dict["pnl_percent"]
        if pnl_pct is not None:
            pnl_pct = float(pnl_pct)
            exit_price = entry * (1 + pnl_pct / 100.0)
        else:
            exit_price = None
        rsi_at_entry = None
        rvol_at_entry = None
        ai_reason = None
        try:
            if row_dict.get("stats_json"):
                stats = json.loads(row_dict["stats_json"])
                rsi_at_entry = stats.get("rsi")
                rvol_at_entry = stats.get("rvol")
        except (json.JSONDecodeError, TypeError):
            pass
        try:
            if row_dict.get("response_json"):
                resp = json.loads(row_dict["response_json"])
                ai_reason = resp.get("reason")
        except (json.JSONDecodeError, TypeError):
            pass
        rows.append({
            "symbol": row_dict["symbol"],
            "strategy_name": row_dict.get("strategy_name"),
            "ai_reason": ai_reason,
            "rsi_at_entry": rsi_at_entry,
            "rvol_at_entry": rvol_at_entry,
            "entry_price": entry,
            "exit_price": exit_price,
            "pnl_usdt": float(row_dict["pnl_usdt"]) if row_dict.get("pnl_usdt") is not None else None,
            "pnl_percent": pnl_pct,
            "close_reason": row_dict.get("close_reason"),
            "opened_at": row_dict.get("opened_at"),
            "closed_at": row_dict.get("closed_at"),
            "mfe_pct": None,
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
