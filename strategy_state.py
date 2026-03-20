"""
Per-symbol strategy selection — stored in DB per user.
Admin (user_id=None or 1) also maintains strategies.json so the live bot
(main.py) can read it without a DB dependency.
Thread-safe.
"""

import json
import os
import threading

import config
import database as db

_VALID = {"trend", "scalp", "sniper", "lsob", "fvg", "auto"}
_lock  = threading.Lock()

ADMIN_FILE = "strategies.json"   # bot (main.py) reads this
ADMIN_UID  = 1


def _defaults() -> dict:
    return dict(zip(config.SYMBOLS, config.STRATEGIES))


def _write_admin_file(strategies: dict):
    """Keep strategies.json in sync when admin changes — bot reads it."""
    try:
        with open(ADMIN_FILE, "w") as f:
            json.dump(strategies, f, indent=2)
    except Exception:
        pass


def load(user_id=None) -> dict:
    """Return {symbol: strategy} for user. Falls back to admin/defaults."""
    uid = user_id or ADMIN_UID
    with _lock:
        saved = db.get_user_strategies(uid)
        defaults = _defaults()

        # If user has no entries yet, seed from admin strategies
        if not saved and uid != ADMIN_UID:
            saved = db.get_user_strategies(ADMIN_UID)

        result = {}
        for sym in config.SYMBOLS:
            result[sym] = saved.get(sym, defaults.get(sym, "trend"))
        return result


def get_strategy(symbol: str, user_id=None) -> str:
    """Returns the current strategy for a symbol."""
    uid = user_id or ADMIN_UID
    with _lock:
        saved = db.get_user_strategies(uid)
        if symbol in saved:
            return saved[symbol]
        # fall back to admin
        admin = db.get_user_strategies(ADMIN_UID)
        return admin.get(symbol, _defaults().get(symbol, "trend"))


def set_strategy(symbol: str, strategy: str, user_id=None) -> bool:
    """Persists a strategy change. Returns False on invalid strategy."""
    if strategy not in _VALID:
        return False
    uid = user_id or ADMIN_UID
    with _lock:
        db.set_user_strategy(uid, symbol, strategy)
        # Keep admin file in sync for the bot
        if uid == ADMIN_UID:
            current = {s: db.get_user_strategies(ADMIN_UID).get(s, "trend")
                       for s in config.SYMBOLS}
            _write_admin_file(current)
    return True
