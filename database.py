"""
SQLite database for trade history.
Stores all trades with entry, exit, PnL, etc.
"""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

DB_PATH = "trades.db"
_lock = threading.Lock()


def init_db():
    """Creates the database and tables if they don't exist yet."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id    TEXT UNIQUE,        -- Bot-generated UUID
                order_id    TEXT,               -- Bitunix order ID
                symbol      TEXT NOT NULL,
                direction   TEXT NOT NULL,      -- 'long' | 'short'
                qty         REAL NOT NULL,      -- Base coin amount
                entry_price REAL NOT NULL,
                exit_price  REAL,
                tp_price    REAL,
                sl_price    REAL,
                entry_time  TEXT NOT NULL,      -- ISO 8601
                exit_time   TEXT,
                pnl_usdt    REAL,               -- Realized PnL in USDT
                status      TEXT NOT NULL DEFAULT 'open',  -- open|closed|tp_hit|sl_hit|error
                rsi_entry   REAL,
                trend_15m   TEXT,
                note        TEXT
            )
        """)
        conn.commit()

        # Migration: add SNIPER partial-TP tracking columns if not present
        _add_column_if_missing(conn, "trades", "strategy",   "TEXT")
        _add_column_if_missing(conn, "trades", "tp1_price",  "REAL")
        _add_column_if_missing(conn, "trades", "tp2_price",  "REAL")
        _add_column_if_missing(conn, "trades", "tp3_price",  "REAL")
        _add_column_if_missing(conn, "trades", "tp1_hit",    "INTEGER DEFAULT 0")
        _add_column_if_missing(conn, "trades", "tp2_hit",    "INTEGER DEFAULT 0")
        _add_column_if_missing(conn, "trades", "tp3_hit",    "INTEGER DEFAULT 0")
        _add_column_if_missing(conn, "trades", "be_moved",   "INTEGER DEFAULT 0")
        conn.commit()


def _add_column_if_missing(conn, table: str, column: str, col_type: str):
    """Safely adds a column to an existing table (no-op if already present)."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except Exception:
        pass  # Column already exists


@contextmanager
def _connect():
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def open_trade(
    trade_id: str,
    order_id: str,
    symbol: str,
    direction: str,
    qty: float,
    entry_price: float,
    tp_price: float,
    sl_price: float,
    rsi_entry: float = None,
    trend_15m: str = None,
    strategy: str = None,
    tp1_price: float = None,
    tp2_price: float = None,
    tp3_price: float = None,
) -> int:
    """Saves a new trade as 'open'. Returns the DB row ID."""
    with _connect() as conn:
        cursor = conn.execute(
            """INSERT INTO trades
               (trade_id, order_id, symbol, direction, qty, entry_price,
                tp_price, sl_price, entry_time, status, rsi_entry, trend_15m,
                strategy, tp1_price, tp2_price, tp3_price)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade_id, order_id, symbol, direction, qty, entry_price,
                tp_price, sl_price,
                datetime.utcnow().isoformat(),
                "open",
                rsi_entry, trend_15m,
                strategy, tp1_price, tp2_price, tp3_price,
            ),
        )
        conn.commit()
        return cursor.lastrowid


def close_trade(trade_id: str, exit_price: float, status: str = "closed"):
    """
    Closes a trade and calculates PnL.
    status: 'closed' | 'tp_hit' | 'sl_hit'
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT direction, qty, entry_price FROM trades WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
        if row is None:
            return

        direction = row["direction"]
        qty = row["qty"]
        entry_price = row["entry_price"]

        if direction == "long":
            pnl = (exit_price - entry_price) * qty
        else:
            pnl = (entry_price - exit_price) * qty

        conn.execute(
            """UPDATE trades
               SET exit_price=?, exit_time=?, pnl_usdt=?, status=?
               WHERE trade_id=?""",
            (
                exit_price,
                datetime.utcnow().isoformat(),
                round(pnl, 4),
                status,
                trade_id,
            ),
        )
        conn.commit()


def get_trade_count() -> int:
    """Returns the total number of trades opened so far."""
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]


def get_trade(trade_id: str) -> dict | None:
    """Returns a single trade by trade_id."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        return dict(row) if row else None


def get_all_trades(limit: int = 200) -> list[dict]:
    """Returns all trades, newest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY entry_time DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_stats() -> dict:
    """Calculates aggregated statistics."""
    with _connect() as conn:
        open_count = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status = 'open'"
        ).fetchone()[0]

        closed = conn.execute(
            "SELECT * FROM trades WHERE status != 'open' AND pnl_usdt IS NOT NULL"
        ).fetchall()

        total_trades = len(closed)
        if total_trades == 0:
            return {
                "total_trades": 0, "open_trades": open_count,
                "win_rate": 0, "total_pnl": 0,
                "avg_win": 0, "avg_loss": 0,
                "best_trade": 0, "worst_trade": 0,
                "long_trades": 0, "short_trades": 0,
            }

        pnls = [r["pnl_usdt"] for r in closed]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        long_count = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE direction='long' AND status != 'open'"
        ).fetchone()[0]

        return {
            "total_trades": total_trades,
            "open_trades": open_count,
            "win_rate": round(len(wins) / total_trades * 100, 1),
            "total_pnl": round(sum(pnls), 2),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
            "best_trade": round(max(pnls), 2),
            "worst_trade": round(min(pnls), 2),
            "long_trades": long_count,
            "short_trades": total_trades - long_count,
        }


def mark_sniper_tp(trade_id: str, tp_num: int):
    """Mark a SNIPER partial TP as hit (tp_num: 1, 2, or 3)."""
    col = f"tp{tp_num}_hit"
    with _connect() as conn:
        conn.execute(f"UPDATE trades SET {col}=1 WHERE trade_id=?", (trade_id,))
        conn.commit()


def mark_sniper_be_moved(trade_id: str, new_sl: float):
    """Record that SL was moved to Break Even after TP1."""
    with _connect() as conn:
        conn.execute(
            "UPDATE trades SET be_moved=1, sl_price=? WHERE trade_id=?",
            (new_sl, trade_id),
        )
        conn.commit()


def get_daily_pnl() -> list[dict]:
    """PnL per day for the chart (last 30 days)."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT DATE(exit_time) as day, SUM(pnl_usdt) as pnl
               FROM trades
               WHERE status != 'open' AND pnl_usdt IS NOT NULL
                 AND exit_time >= DATE('now', '-30 days')
               GROUP BY DATE(exit_time)
               ORDER BY day ASC"""
        ).fetchall()
        return [dict(r) for r in rows]
