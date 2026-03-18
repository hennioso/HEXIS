"""
SQLite database for trade history and user accounts.
"""

import sqlite3
import threading
import base64
import hashlib
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from cryptography.fernet import Fernet

DB_PATH = "trades.db"
_lock = threading.Lock()


def _get_fernet() -> Fernet:
    """Derive a stable Fernet key from HEXIS_ENCRYPTION_KEY or FLASK_SECRET_KEY."""
    raw = os.getenv("HEXIS_ENCRYPTION_KEY") or os.getenv("FLASK_SECRET_KEY") or "hexis-default-key-change-in-prod"
    key_bytes = hashlib.sha256(raw.encode()).digest()   # 32 bytes
    return Fernet(base64.urlsafe_b64encode(key_bytes))


def _encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def _decrypt(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()


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
        _add_column_if_missing(conn, "trades", "be_moved",        "INTEGER DEFAULT 0")
        _add_column_if_missing(conn, "trades", "unrealized_pnl",  "REAL")
        _add_column_if_missing(conn, "trades", "partial_pnl_usdt","REAL")
        _add_column_if_missing(conn, "trades", "margin_usdt",      "REAL")
        _add_column_if_missing(conn, "trades", "leverage",         "INTEGER")
        _add_column_if_missing(conn, "trades", "trail_activated",  "INTEGER DEFAULT 0")
        _add_column_if_missing(conn, "trades", "trail_stop",       "REAL")
        _add_column_if_missing(conn, "trades", "user_id",          "INTEGER")

        # One-time migration: retag mislabelled scalp trades
        # (trades opened before strategy tag was passed explicitly kept trend_15m='scalp')
        conn.execute("""
            UPDATE trades
            SET strategy = 'scalp'
            WHERE trend_15m = 'scalp' AND (strategy = 'trend' OR strategy IS NULL)
        """)
        conn.commit()

        # ── Invite codes table ───────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invite_codes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                code       TEXT UNIQUE NOT NULL,
                email      TEXT,           -- recipient email (for display)
                used       INTEGER DEFAULT 0,
                used_by    INTEGER,        -- user_id that redeemed it
                created_at TEXT NOT NULL,
                expires_at TEXT            -- NULL = never expires
            )
        """)
        conn.commit()

        # ── Users table ──────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                username       TEXT UNIQUE NOT NULL,
                email          TEXT,
                password_hash  TEXT NOT NULL,
                api_key_enc    TEXT,        -- Fernet-encrypted Bitunix API key
                secret_key_enc TEXT,        -- Fernet-encrypted Bitunix secret
                is_admin       INTEGER DEFAULT 0,
                is_active      INTEGER DEFAULT 1,
                created_at     TEXT NOT NULL
            )
        """)
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
    For SNIPER trades with partial TPs, the remaining qty is computed from
    which TP levels were hit; partial_pnl_usdt is added to the final PnL.
    """
    with _connect() as conn:
        row = conn.execute(
            """SELECT direction, qty, entry_price,
                      tp1_hit, tp2_hit, tp3_hit, partial_pnl_usdt
               FROM trades WHERE trade_id = ?""",
            (trade_id,),
        ).fetchone()
        if row is None:
            return

        direction   = row["direction"]
        qty         = row["qty"]
        entry_price = row["entry_price"]
        partial_pnl = row["partial_pnl_usdt"] or 0.0

        # For SNIPER partial closes: only the remaining fraction is closed here
        closed_frac = (
            0.20 * (row["tp1_hit"] or 0)
            + 0.50 * (row["tp2_hit"] or 0)
            + 0.25 * (row["tp3_hit"] or 0)
        )
        remaining_qty = qty * (1.0 - closed_frac)

        if direction == "long":
            pnl = (exit_price - entry_price) * remaining_qty + partial_pnl
        else:
            pnl = (entry_price - exit_price) * remaining_qty + partial_pnl

        conn.execute(
            """UPDATE trades
               SET exit_price=?, exit_time=?, pnl_usdt=?, status=?, unrealized_pnl=NULL
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


def get_all_trades(limit: int = 200, user_id: int = None) -> list[dict]:
    """Returns all trades, newest first. Optionally filtered by user_id."""
    with _connect() as conn:
        if user_id is not None:
            rows = conn.execute(
                "SELECT * FROM trades WHERE user_id = ? ORDER BY entry_time DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY entry_time DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def get_stats(user_id: int = None) -> dict:
    """Calculates aggregated statistics, optionally filtered by user_id."""
    uid_filter = "AND user_id = ?" if user_id is not None else ""
    uid_args   = (user_id,) if user_id is not None else ()
    with _connect() as conn:
        open_count = conn.execute(
            f"SELECT COUNT(*) FROM trades WHERE status = 'open' {uid_filter}", uid_args
        ).fetchone()[0]

        closed = conn.execute(
            f"SELECT * FROM trades WHERE status != 'open' AND pnl_usdt IS NOT NULL {uid_filter}",
            uid_args,
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


def update_trade_qty(trade_id: str, qty: float):
    """Updates the qty of an open trade (e.g. after manual position sizing on exchange)."""
    with _connect() as conn:
        conn.execute("UPDATE trades SET qty=? WHERE trade_id=?", (qty, trade_id))
        conn.commit()


def update_unrealized_pnl(trade_id: str, pnl: float):
    """Updates the live unrealized PnL for an open trade."""
    with _connect() as conn:
        conn.execute(
            "UPDATE trades SET unrealized_pnl=? WHERE trade_id=?",
            (round(pnl, 4), trade_id),
        )
        conn.commit()


def update_trade_margin(trade_id: str, margin_usdt: float, leverage: int):
    """Stores margin and leverage from the exchange position."""
    with _connect() as conn:
        conn.execute(
            "UPDATE trades SET margin_usdt=?, leverage=? WHERE trade_id=?",
            (round(margin_usdt, 4), leverage, trade_id),
        )
        conn.commit()


def update_trade_field(row_id: int, field: str, value):
    """Generic update of a single column by DB row id (internal use only)."""
    _ALLOWED = {"note", "strategy", "tp_price", "sl_price"}
    if field not in _ALLOWED:
        raise ValueError(f"Field '{field}' not allowed for generic update")
    with _connect() as conn:
        conn.execute(f"UPDATE trades SET {field}=? WHERE id=?", (value, row_id))
        conn.commit()


def add_partial_pnl(trade_id: str, pnl: float):
    """Adds realized PnL from a partial close to the running total."""
    with _connect() as conn:
        conn.execute(
            "UPDATE trades SET partial_pnl_usdt = COALESCE(partial_pnl_usdt, 0) + ? WHERE trade_id=?",
            (round(pnl, 4), trade_id),
        )
        conn.commit()


def mark_sniper_tp(trade_id: str, tp_num: int):
    """Mark a SNIPER partial TP as hit (tp_num: 1, 2, or 3)."""
    col = f"tp{tp_num}_hit"
    with _connect() as conn:
        conn.execute(f"UPDATE trades SET {col}=1 WHERE trade_id=?", (trade_id,))
        conn.commit()


def correct_closed_trade(trade_id: str, exit_price: float, pnl_usdt: float, close_time: str = None):
    """
    Overwrite exit_price and pnl_usdt for a trade with exchange-sourced data.
    Used by the hourly closed-trade sync. close_time is optional (ISO string).
    """
    with _connect() as conn:
        conn.execute(
            """UPDATE trades
               SET exit_price=?, pnl_usdt=?,
                   exit_time=COALESCE(?, exit_time),
                   status=CASE WHEN status='open' THEN 'closed' ELSE status END,
                   unrealized_pnl=NULL
               WHERE trade_id=?""",
            (exit_price, round(pnl_usdt, 4), close_time, trade_id),
        )
        conn.commit()


def set_trail_stop(trade_id: str, trail_price: float):
    """Activate or update the trailing stop price for a trade."""
    with _connect() as conn:
        conn.execute(
            "UPDATE trades SET trail_activated=1, trail_stop=? WHERE trade_id=?",
            (round(trail_price, 8), trade_id),
        )
        conn.commit()


def get_hourly_stats() -> list[dict]:
    """Win rate and trade count per UTC hour (0–23). Used by AI Trade Analyst."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT
                   CAST(strftime('%H', exit_time) AS INTEGER) AS hour,
                   COUNT(*) AS trades,
                   SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins
               FROM trades
               WHERE status != 'open' AND pnl_usdt IS NOT NULL AND exit_time IS NOT NULL
               GROUP BY hour
               ORDER BY hour"""
        ).fetchall()
        return [
            {
                "hour_utc": r["hour"],
                "trades":   r["trades"],
                "win_rate": round(r["wins"] / r["trades"] * 100, 1),
            }
            for r in rows
        ]


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


def get_today_pnl() -> float:
    """Sum of realized PnL for today (UTC date)."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(pnl_usdt), 0)
               FROM trades
               WHERE status != 'open'
                 AND pnl_usdt IS NOT NULL
                 AND DATE(exit_time) = DATE('now')"""
        ).fetchone()
        return float(row[0])


def get_equity_curve() -> list[dict]:
    """
    Returns cumulative PnL over time — one row per closed trade,
    sorted by exit_time ascending. Used for the equity-curve chart.
    """
    with _connect() as conn:
        rows = conn.execute(
            """SELECT exit_time, pnl_usdt, symbol, direction, strategy
               FROM trades
               WHERE status != 'open' AND pnl_usdt IS NOT NULL
               ORDER BY exit_time ASC"""
        ).fetchall()
        result = []
        running = 0.0
        for r in rows:
            running += r["pnl_usdt"]
            result.append({
                "time":      r["exit_time"],
                "pnl":       round(r["pnl_usdt"], 4),
                "cumulative": round(running, 4),
                "symbol":    r["symbol"],
                "direction": r["direction"],
                "strategy":  r["strategy"],
            })
        return result


def get_analytics() -> dict:
    """
    Per-strategy and per-symbol breakdown + drawdown metrics.
    """
    with _connect() as conn:
        closed = conn.execute(
            """SELECT symbol, direction, strategy, pnl_usdt, exit_time
               FROM trades
               WHERE status != 'open' AND pnl_usdt IS NOT NULL
               ORDER BY exit_time ASC"""
        ).fetchall()

        if not closed:
            return {"by_strategy": [], "by_symbol": [], "drawdown": {}}

        # ---- Per-strategy ----
        strat_data: dict[str, list] = {}
        for r in closed:
            key = r["strategy"] or "trend"   # legacy trades without strategy tag → trend
            strat_data.setdefault(key, []).append(r["pnl_usdt"])

        by_strategy = []
        for strat, pnls in sorted(strat_data.items()):
            wins   = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            by_strategy.append({
                "strategy":    strat,
                "trades":      len(pnls),
                "win_rate":    round(len(wins) / len(pnls) * 100, 1),
                "total_pnl":   round(sum(pnls), 2),
                "avg_pnl":     round(sum(pnls) / len(pnls), 2),
                "avg_win":     round(sum(wins)   / len(wins),   2) if wins   else 0,
                "avg_loss":    round(sum(losses)  / len(losses), 2) if losses else 0,
            })

        # ---- Per-symbol ----
        sym_data: dict[str, list] = {}
        for r in closed:
            sym_data.setdefault(r["symbol"], []).append(r["pnl_usdt"])

        by_symbol = []
        for sym, pnls in sorted(sym_data.items()):
            wins = [p for p in pnls if p > 0]
            by_symbol.append({
                "symbol":    sym,
                "trades":    len(pnls),
                "win_rate":  round(len(wins) / len(pnls) * 100, 1),
                "total_pnl": round(sum(pnls), 2),
                "avg_pnl":   round(sum(pnls) / len(pnls), 2),
            })

        # ---- Max Drawdown ----
        pnls_list = [r["pnl_usdt"] for r in closed]
        peak = 0.0
        max_dd = 0.0
        running = 0.0
        for p in pnls_list:
            running += p
            if running > peak:
                peak = running
            dd = peak - running
            if dd > max_dd:
                max_dd = dd

        total_pnl = sum(pnls_list)
        wins_all  = [p for p in pnls_list if p > 0]

        return {
            "by_strategy": by_strategy,
            "by_symbol":   by_symbol,
            "drawdown": {
                "max_drawdown_usdt": round(max_dd, 2),
                "total_pnl":        round(total_pnl, 2),
            },
        }


# ── User management ──────────────────────────────────────────────────────────

def create_user(username: str, password_hash: str, email: str = None, is_admin: bool = False) -> int:
    """Insert a new user. Returns the new row id."""
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO users (username, email, password_hash, is_admin, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (username, email, password_hash, 1 if is_admin else 0, datetime.utcnow().isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def get_user_by_username(username: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_all_users() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT id, username, email, is_admin, is_active, created_at FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def count_users() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def update_user_api_keys(user_id: int, api_key: str, secret_key: str):
    """Store Fernet-encrypted Bitunix credentials for a user."""
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET api_key_enc = ?, secret_key_enc = ? WHERE id = ?",
            (_encrypt(api_key), _encrypt(secret_key), user_id),
        )
        conn.commit()


def get_users_with_api_keys() -> list[dict]:
    """Return all active users that have API keys set, with decrypted keys."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE is_active = 1 AND api_key_enc IS NOT NULL AND secret_key_enc IS NOT NULL"
        ).fetchall()
    result = []
    for row in rows:
        try:
            result.append({
                "id":       row["id"],
                "username": row["username"],
                "api_key":  _decrypt(row["api_key_enc"]),
                "secret":   _decrypt(row["secret_key_enc"]),
            })
        except Exception:
            pass  # skip users with corrupted/old encrypted keys
    return result


# ── Invite-code management ────────────────────────────────────────────────────

def create_invite_code(code: str, email: str = None, expires_at: str = None) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO invite_codes (code, email, created_at, expires_at) VALUES (?,?,?,?)",
            (code, email, datetime.utcnow().isoformat(), expires_at),
        )
        conn.commit()
        return cur.lastrowid


def get_invite_code(code: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM invite_codes WHERE code = ?", (code,)).fetchone()
        return dict(row) if row else None


def use_invite_code(code: str, user_id: int):
    with _connect() as conn:
        conn.execute(
            "UPDATE invite_codes SET used = 1, used_by = ? WHERE code = ?",
            (user_id, code),
        )
        conn.commit()


def get_all_invite_codes() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM invite_codes ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
