"""
SQLite persistence for orders (trades) and balance.
Used by Executor (insert order, sync balance) and Monitor (close order, update balance).
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, List, Optional

# Default path: ./data/algotrader.db (or /data/algotrader.db in Docker with volume)
DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "algotrader.db")


def get_database_path() -> str:
    return os.getenv("DATABASE_PATH", DEFAULT_DB_PATH)


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
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_orders_symbol_status ON orders(symbol, status);
        CREATE INDEX IF NOT EXISTS idx_orders_opened_at ON orders(opened_at);

        CREATE TABLE IF NOT EXISTS balance (
            currency TEXT PRIMARY KEY,
            amount REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)


def insert_order(
    conn: sqlite3.Connection,
    symbol: str,
    side: str,
    amount_usdt: float,
    entry_price: float,
    quantity: float,
    tp_price: float,
    sl_price: float,
    exchange_order_id: Optional[str] = None,
) -> int:
    now = datetime.utcnow().isoformat() + "Z"
    cur = conn.execute(
        """INSERT INTO orders (symbol, side, amount_usdt, entry_price, quantity, tp_price, sl_price, status, exchange_order_id, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
        (symbol, side, amount_usdt, entry_price, quantity, tp_price, sl_price, exchange_order_id, now),
    )
    return cur.lastrowid


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
        """SELECT id, symbol, entry_price, quantity, tp_price, sl_price, amount_usdt, opened_at
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
) -> None:
    now = datetime.utcnow().isoformat() + "Z"
    conn.execute(
        """UPDATE orders SET status = 'closed', closed_at = ?, pnl_usdt = ?, pnl_percent = ?, close_reason = ?
           WHERE id = ?""",
        (now, pnl_usdt, pnl_percent, close_reason, order_id),
    )


def get_balance(conn: sqlite3.Connection, currency: str) -> float:
    row = conn.execute("SELECT amount FROM balance WHERE currency = ?", (currency,)).fetchone()
    return float(row["amount"]) if row else 0.0


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
