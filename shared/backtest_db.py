"""
SQLite abstraction for backtest.db.

Completely separate from the live algotrader.db — the two databases never interact.
All backtest runs, simulated trades, and cached historical OHLCV live here.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DEFAULT_BACKTEST_DB_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "backtest.db"
)


def get_backtest_db_path() -> str:
    return os.getenv("BACKTEST_DB_PATH", DEFAULT_BACKTEST_DB_PATH)


@contextmanager
def get_connection():
    path = get_backtest_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_runs (
            id TEXT PRIMARY KEY,
            bot_version TEXT,
            strategy TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            initial_balance REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            progress_pct REAL NOT NULL DEFAULT 0,
            symbols_total INTEGER NOT NULL DEFAULT 0,
            symbols_done INTEGER NOT NULL DEFAULT 0,
            trades_count INTEGER,
            win_count INTEGER,
            win_rate REAL,
            profit_factor REAL,
            sharpe REAL,
            max_drawdown REAL,
            final_balance REAL,
            net_pnl_usdt REAL,
            net_pnl_pct REAL,
            avg_hold_hours REAL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            error_message TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL DEFAULT 'long',
            entry_price REAL NOT NULL,
            exit_price REAL,
            sl_price REAL,
            tp_price REAL,
            entry_time TEXT NOT NULL,
            exit_time TEXT,
            hours_held REAL,
            pnl_usdt REAL,
            pnl_pct REAL,
            close_reason TEXT,
            strategy TEXT,
            bot_version TEXT,
            confidence INTEGER,
            setup_grade TEXT,
            verdict_reason TEXT,
            FOREIGN KEY (run_id) REFERENCES backtest_runs(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ohlcv (
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            PRIMARY KEY (symbol, timeframe, timestamp)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ohlcv_sym_tf_ts ON ohlcv(symbol, timeframe, timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_backtest_trades_run ON backtest_trades(run_id)")


# ── Run management ─────────────────────────────────────────────────────────────

def create_run(
    conn, run_id: str, bot_version: str, strategy: str,
    start_date: str, end_date: str, initial_balance: float
) -> None:
    conn.execute(
        """INSERT INTO backtest_runs
           (id, bot_version, strategy, start_date, end_date, initial_balance,
            status, progress_pct, symbols_total, symbols_done, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, 0, 0, ?)""",
        (run_id, bot_version, strategy, start_date, end_date,
         initial_balance, datetime.utcnow().isoformat()),
    )


def update_run_status(conn, run_id: str, status: str, error_message: str = None) -> None:
    if status in ("completed", "error"):
        conn.execute(
            "UPDATE backtest_runs SET status=?, error_message=?, completed_at=? WHERE id=?",
            (status, error_message, datetime.utcnow().isoformat(), run_id),
        )
    else:
        conn.execute(
            "UPDATE backtest_runs SET status=?, error_message=? WHERE id=?",
            (status, error_message, run_id),
        )


def update_run_progress(
    conn, run_id: str, symbols_done: int, symbols_total: int, status: str = None
) -> None:
    pct = round(symbols_done / symbols_total * 100, 1) if symbols_total > 0 else 0
    if status:
        conn.execute(
            "UPDATE backtest_runs SET symbols_done=?, symbols_total=?, progress_pct=?, status=? WHERE id=?",
            (symbols_done, symbols_total, pct, status, run_id),
        )
    else:
        conn.execute(
            "UPDATE backtest_runs SET symbols_done=?, symbols_total=?, progress_pct=? WHERE id=?",
            (symbols_done, symbols_total, pct, run_id),
        )


def update_run_metrics(
    conn, run_id: str, trades_count: int, win_count: int,
    profit_factor, sharpe, max_drawdown, final_balance: float,
    initial_balance: float, avg_hold_hours
) -> None:
    net_pnl = final_balance - initial_balance
    net_pct = round(net_pnl / initial_balance * 100, 2) if initial_balance > 0 else 0
    win_rate = round(win_count / trades_count * 100, 1) if trades_count > 0 else 0
    conn.execute(
        """UPDATE backtest_runs SET
           trades_count=?, win_count=?, win_rate=?, profit_factor=?, sharpe=?,
           max_drawdown=?, final_balance=?, net_pnl_usdt=?, net_pnl_pct=?,
           avg_hold_hours=?
           WHERE id=?""",
        (trades_count, win_count,
         win_rate, profit_factor, sharpe, max_drawdown,
         round(final_balance, 2), round(net_pnl, 2), net_pct,
         avg_hold_hours, run_id),
    )


def get_run(conn, run_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM backtest_runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def get_active_run(conn) -> dict | None:
    """Return the most recent non-completed run (running_fetch or running_sim)."""
    row = conn.execute(
        "SELECT * FROM backtest_runs WHERE status IN ('pending','running_fetch','running_sim') "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def get_all_runs(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM backtest_runs ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# ── Trade management ───────────────────────────────────────────────────────────

def insert_trade(
    conn, run_id: str, symbol: str, entry_price: float, exit_price: float,
    sl_price: float, tp_price: float, entry_time: str, exit_time: str,
    hours_held: float, pnl_usdt: float, pnl_pct: float, close_reason: str,
    strategy: str, bot_version: str, confidence: int = None,
    setup_grade: str = None, verdict_reason: str = None,
) -> None:
    conn.execute(
        """INSERT INTO backtest_trades
           (run_id, symbol, side, entry_price, exit_price, sl_price, tp_price,
            entry_time, exit_time, hours_held, pnl_usdt, pnl_pct, close_reason,
            strategy, bot_version, confidence, setup_grade, verdict_reason)
           VALUES (?, ?, 'long', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, symbol, entry_price, exit_price, sl_price, tp_price,
         entry_time, exit_time, round(hours_held, 2),
         round(pnl_usdt, 4), round(pnl_pct, 4),
         close_reason, strategy, bot_version, confidence, setup_grade, verdict_reason),
    )


def get_trades_for_run(conn, run_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM backtest_trades WHERE run_id=? ORDER BY entry_time ASC",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── OHLCV cache ────────────────────────────────────────────────────────────────

def store_candles(conn, symbol: str, timeframe: str, candles: list) -> int:
    """Insert candles (list of [ts, o, h, l, c, v] or [ts, o, h, l, c, v, ...]).

    Uses INSERT OR IGNORE to avoid duplicates. Returns count of new rows inserted.
    """
    count = 0
    for c in candles:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO ohlcv (symbol, timeframe, timestamp, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (symbol, timeframe, int(c[0]), float(c[1]), float(c[2]),
                 float(c[3]), float(c[4]), float(c[5])),
            )
            count += 1
        except Exception:
            pass
    return count


def load_candles(conn, symbol: str, timeframe: str) -> list:
    """Load all cached candles sorted by timestamp. Returns list of tuples."""
    rows = conn.execute(
        "SELECT timestamp, open, high, low, close, volume FROM ohlcv "
        "WHERE symbol=? AND timeframe=? ORDER BY timestamp ASC",
        (symbol, timeframe),
    ).fetchall()
    return [tuple(r) for r in rows]


def has_candles(conn, symbol: str, timeframe: str, start_ts: int, end_ts: int) -> bool:
    """Check if we have sufficient data coverage for the given date range."""
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM ohlcv WHERE symbol=? AND timeframe=? "
        "AND timestamp >= ? AND timestamp <= ?",
        (symbol, timeframe, start_ts, end_ts),
    ).fetchone()
    return row["cnt"] > 100 if row else False
