"""
SQLite Datenbank für Trade-History.
Speichert alle Trades mit Entry, Exit, PnL etc.
"""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

DB_PATH = "trades.db"
_lock = threading.Lock()


def init_db():
    """Erstellt die Datenbank und Tabellen falls noch nicht vorhanden."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id    TEXT UNIQUE,        -- Bot-generierte UUID
                order_id    TEXT,               -- Bitunix Order ID
                symbol      TEXT NOT NULL,
                direction   TEXT NOT NULL,      -- 'long' | 'short'
                qty         REAL NOT NULL,      -- BTC-Menge
                entry_price REAL NOT NULL,
                exit_price  REAL,
                tp_price    REAL,
                sl_price    REAL,
                entry_time  TEXT NOT NULL,      -- ISO 8601
                exit_time   TEXT,
                pnl_usdt    REAL,               -- Realisierter PnL in USDT
                status      TEXT NOT NULL DEFAULT 'open',  -- open|closed|tp_hit|sl_hit|error
                rsi_entry   REAL,
                trend_15m   TEXT,
                note        TEXT
            )
        """)
        conn.commit()


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
) -> int:
    """Speichert einen neuen Trade als 'open'. Gibt die DB-ID zurück."""
    with _connect() as conn:
        cursor = conn.execute(
            """INSERT INTO trades
               (trade_id, order_id, symbol, direction, qty, entry_price,
                tp_price, sl_price, entry_time, status, rsi_entry, trend_15m)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade_id, order_id, symbol, direction, qty, entry_price,
                tp_price, sl_price,
                datetime.utcnow().isoformat(),
                "open",
                rsi_entry, trend_15m,
            ),
        )
        conn.commit()
        return cursor.lastrowid


def close_trade(trade_id: str, exit_price: float, status: str = "closed"):
    """
    Schließt einen Trade und berechnet den PnL.
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
    """Gibt die Gesamtanzahl aller bisher geöffneten Trades zurück."""
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]


def get_trade(trade_id: str) -> dict | None:
    """Gibt einen einzelnen Trade anhand der trade_id zurück."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        return dict(row) if row else None


def get_all_trades(limit: int = 200) -> list[dict]:
    """Gibt alle Trades zurück, neueste zuerst."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY entry_time DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_stats() -> dict:
    """Berechnet aggregierte Statistiken."""
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


def get_daily_pnl() -> list[dict]:
    """PnL pro Tag für das Chart (letzte 30 Tage)."""
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
